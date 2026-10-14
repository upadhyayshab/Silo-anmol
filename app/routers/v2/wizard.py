from typing import Optional

from fastapi import APIRouter, Depends, Query

from config import get_engine, get_settings
from dependencies.auth import require_jwt
from managers.cropManagers import CropManager
from models.wizardModels import (
    FarmContext, WizardQuestion, WizardQuestionOption, WizardQuestionsResponse,
)
from services.agroInternalService import AgroInternalService

import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from models.recommendationModels import RecommendationRequest
from models.wizardModels import WizardRecommendationItem, WizardSubmitRequest, WizardSubmitResponse
from services.kafka_client import publish_event
from utils.scoringEngine import CropScoringEngine

router = APIRouter(tags=["Wizard v2"])
settings = get_settings()
engine = get_engine(settings.name)

crop_manager = CropManager(engine)
agro_internal_service = AgroInternalService(
    base_url=settings.agro_internal_base_url,
    shared_secret=settings.internal_service_secret,
)

logger = logging.getLogger("wizard_router")
scoring_engine = CropScoringEngine(engine)

ALL_QUESTIONS: list[WizardQuestion] = [
    WizardQuestion(id="location", type="map_pin", label="Where is your farm?", required=True),
    WizardQuestion(
        id="season", type="radio", label="Which season are you planning to sow for?",
        options=[
            WizardQuestionOption(label="Kharif (Monsoon: Jun - Oct)", value="Kharif"),
            WizardQuestionOption(label="Rabi (Winter: Oct - Mar)", value="Rabi"),
            WizardQuestionOption(label="Summer / Zaid (Mar - Jun)", value="Summer"),
            WizardQuestionOption(label="Perennial / Annual", value="Annual"),
        ],
    ),
    WizardQuestion(
        id="has_active_crop", type="radio", label="Do you currently have a crop growing?",
        options=[WizardQuestionOption(label="Yes", value="Yes"), WizardQuestionOption(label="No", value="No")],
    ),
    WizardQuestion(id="crop", type="search", label="Which crop is growing?", required=False, description="Shown only if has_active_crop=Yes"),
    WizardQuestion(id="sowing_date", type="date", label="When did you sow?", required=False, description="Shown only if has_active_crop=Yes"),
    WizardQuestion(id="previous_crop", type="search", label="What was grown here before?", required=False),
    WizardQuestion(
        id="irrigation", type="multi_select", label="How do you water this field?",
        options=[
            WizardQuestionOption(label="Rainfed", value="Rainfed"),
            WizardQuestionOption(label="Surface (flood/canal/furrow)", value="Surface"),
            WizardQuestionOption(label="Drip", value="Drip"),
            WizardQuestionOption(label="Sprinkler", value="Sprinkler"),
        ],
    ),
    WizardQuestion(
        id="budget", type="radio", label="What is your planned investment capacity per acre?",
        options=[
            WizardQuestionOption(label="Low (< ₹15,000 / acre)", value="low"),
            WizardQuestionOption(label="Medium (₹15,000 - ₹35,000 / acre)", value="medium"),
            WizardQuestionOption(label="High (> ₹35,000 / acre)", value="high"),
        ],
    ),
    WizardQuestion(
        id="livestock", type="radio", label="Do you have livestock on your farm?",
        options=[
            WizardQuestionOption(label="Cattle / Buffaloes", value="cattle"),
            WizardQuestionOption(label="Goats / Sheep", value="small_ruminants"),
            WizardQuestionOption(label="No livestock", value="none"),
        ],
    ),
    WizardQuestion(id="farm_name", type="text", label="Give your farm a name", required=False),
]

# TRD §8.2 suppression logic, keyed by which farm field (on the Agro internal
# response) implies the question is already answered.
SUPPRESSION_RULES: list[tuple[str, list[str]]] = [
    ("center", ["location"]),
    ("name", ["farm_name"]),
    ("crop", ["has_active_crop", "crop", "sowing_date"]),
    ("previous_crop", ["previous_crop"]),
    ("irrigation_methods", ["irrigation"]),
]


@router.get("/wizard/questions", response_model=WizardQuestionsResponse, dependencies=[Depends(require_jwt)])
async def get_wizard_questions(farmId: Optional[str] = Query(None)):
    farm_context = None
    suppressed_ids: set[str] = set()

    if farmId:
        farm = await agro_internal_service.get_farm(farmId, user_id="")
        if farm:
            farm_context = FarmContext(
                farm_id=farm.get("farm_id") or farm.get("id"),
                name=farm.get("name"),
                location=farm.get("location") or farm.get("center"),
                crop=farm.get("crop"),
                has_active_crop=bool(farm.get("has_active_crop")),
                irrigation=farm.get("irrigation") or farm.get("irrigation_methods"),
                boundary_status=farm.get("boundary_status"),
            )
            for farm_field, suppressed in SUPPRESSION_RULES:
                if farm.get(farm_field):
                    suppressed_ids.update(suppressed)

    questions = [q for q in ALL_QUESTIONS if q.id not in suppressed_ids]
    return WizardQuestionsResponse(farm_context=farm_context, questions=questions)


_SEASON_MAP = {"kharif": "KHARIF", "rabi": "RABI", "summer": "ZAID", "annual": "LONGTERM"}
_BUDGET_MAP = {"low": "B1", "medium": "B3", "high": "B4"}


def _map_season(season: str) -> str:
    mapped = _SEASON_MAP.get(season.lower())
    if mapped is None:
        raise ValueError(f"Unrecognized season '{season}'. Expected one of: Kharif, Rabi, Summer, Annual")
    return mapped


def _map_budget(budget: str) -> str:
    mapped = _BUDGET_MAP.get(budget.lower())
    if mapped is None:
        raise ValueError(f"Unrecognized budget '{budget}'. Expected one of: low, medium, high")
    return mapped


def _map_irrigation(methods: list[str]) -> list[str]:
    return [m.upper() for m in methods]


@router.post("/wizard/submit", response_model=WizardSubmitResponse)
async def submit_wizard(payload: WizardSubmitRequest, auth_payload: dict = Depends(require_jwt)):
    """TRD §8.6/§8.7: synchronous ML recommendation + async CropGuideCompleted
    emission. The Kafka publish never blocks or fails the HTTP response
    (Review Focus) — it's fired and its result is only logged."""
    upid = auth_payload["upid"]
    answers = payload.answers
    farm = None
    if payload.farmId:
        farm = await agro_internal_service.get_farm(payload.farmId, user_id=upid)

    location = answers.location
    if location is None and farm:
        location = farm.get("location") or farm.get("center")
    if location is None:
        raise HTTPException(status_code=400, detail="answers.location is required when farmId is not provided or the farm has no boundary yet")

    try:
        season = _map_season(answers.season)
        budget_bucket = _map_budget(answers.budget) if answers.budget else "B3"
        irrigation = _map_irrigation(answers.irrigation) if answers.irrigation else (
            _map_irrigation(farm.get("irrigation") or farm.get("irrigation_methods") or []) if farm else []
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not irrigation:
        raise HTTPException(status_code=400, detail="answers.irrigation is required (at least one method)")

    previous_crop = answers.previous_crop or (farm.get("previous_crop") if farm else None)
    was_fallow = previous_crop is None

    previous_crop_family = None
    if not was_fallow:
        # Same lookup-with-fallback as the existing /recommend handler
        # (app/routers/v1/recommend.py) — exact match first, then a
        # case-insensitive scan, then a clear 400 rather than letting
        # RecommendationRequest's validator raise on a None family.
        crops_res = await crop_manager.fetch_all(limit=1, filters={"name_en": previous_crop})
        if crops_res.items:
            previous_crop_family = crops_res.items[0].family
        else:
            all_crops = await crop_manager.fetch_all(limit=0)
            matched = next((c for c in all_crops.items if c.name_en.lower() == previous_crop.lower()), None)
            if not matched:
                raise HTTPException(status_code=400, detail=f"previous_crop '{previous_crop}' not found in crop database")
            previous_crop_family = matched.family

    rec_request = RecommendationRequest(
        lat=location[1], lon=location[0],
        season=season, water_methods=irrigation, budget_bucket=budget_bucket,
        previous_crop_family=previous_crop_family, was_fallow=was_fallow, top_n=5,
    )
    response = await scoring_engine.recommend(rec_request, auto_detect_soil=True)

    recommendations = [
        WizardRecommendationItem(
            id=f"rec_{i}", crop_name=r.crop, suitability_score=round(r.suitability_pct),
            roi_estimate="High" if r.suitability_pct >= 80 else "Medium" if r.suitability_pct >= 50 else "Low",
            duration_days=None, water_requirement=None,
            reason=f"Suitability {r.suitability_pct:.0f}% for {season.title()} season.",
        )
        for i, r in enumerate(response.results)
    ]

    # Awaited, not fire-and-forget: the try/except inside already guarantees
    # a Kafka outage never raises into this response (Review Focus); awaiting
    # here (instead of asyncio.create_task) keeps the call deterministic for
    # tests and avoids a task that can outlive the request's own lifecycle.
    await _publish_crop_guide_completed(upid, payload, farm, season, budget_bucket)

    return WizardSubmitResponse(recommendations=recommendations)


async def _publish_crop_guide_completed(
    upid: str, payload: WizardSubmitRequest, farm: dict | None, season: str, budget_bucket: str
) -> None:
    """Fire-and-forget: logs on failure, never raises into the caller."""
    answers = payload.answers
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "CropGuideCompleted",
        "version": "1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": upid,
        "farm_id": payload.farmId,
        "data": {
            "farm_name": answers.farm_name,
            "center": answers.location or (farm.get("center") if farm else None),
            "has_active_crop": (answers.has_active_crop or "").lower() == "yes",
            "crop": answers.crop,
            "sowing_date": answers.sowing_date,
            "previous_crop": answers.previous_crop,
            "irrigation_methods": answers.irrigation,
            "season": season,
            "budget": budget_bucket,
            "livestock": answers.livestock,
        },
    }
    try:
        await publish_event("crop_guide_completed", str(event), key=payload.farmId or event["event_id"])
    except Exception:
        logger.exception("Failed to publish CropGuideCompleted event_id=%s", event["event_id"])
