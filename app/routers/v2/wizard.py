from typing import Optional

from fastapi import APIRouter, Depends, Query

from config import get_engine, get_settings
from dependencies.auth import require_jwt
from managers.cropManagers import CropManager, RecommendationHistoryManager, RecommendationHistorySchema
from managers.cropReportManagers import UserCropReportManager, UserCropReportSchema
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
from services.eventBrokerService import EventBrokerService
from services.subscriptionService import SubscriptionService
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
subscription_service = SubscriptionService()
event_broker = EventBrokerService()
history_manager = RecommendationHistoryManager(engine)
user_crop_report_manager = UserCropReportManager(engine)

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


@router.get("/wizard/questions", response_model=WizardQuestionsResponse)
async def get_wizard_questions(farmId: Optional[str] = Query(None), auth_payload: dict = Depends(require_jwt)):
    upid = auth_payload["upid"]
    farm_context = None
    suppressed_ids: set[str] = set()

    if farmId:
        farm = await agro_internal_service.get_farm(farmId, upid)
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


_VALID_WATER_METHODS = {"RAINFED", "SURFACE", "DRIP", "SPRINKLER"}


def _map_irrigation(methods: list[str]) -> list[str]:
    mapped = [m.upper() for m in methods]
    unknown = [m for m in mapped if m not in _VALID_WATER_METHODS]
    if unknown:
        raise ValueError(
            f"Unrecognized irrigation method(s) {unknown}. Expected one of: Rainfed, Surface, Drip, Sprinkler"
        )
    return mapped


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
    if location is None and not answers.pincode:
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

    # DEF-04: when the farm has an active standing crop, THAT crop is the real
    # rotation predecessor for "what to plant next" — not the wizard's
    # previous_crop answer (which describes what was grown before the
    # standing crop, if anything). Never written back to the farm — this
    # only feeds the scoring engine's rotation input.
    if farm and farm.get("has_active_crop") and farm.get("crop"):
        rotation_crop_name = farm["crop"]
    else:
        rotation_crop_name = answers.previous_crop or (farm.get("previous_crop") if farm else None)
    was_fallow = rotation_crop_name is None

    previous_crop_family = None
    if not was_fallow:
        # Same lookup-with-fallback as the existing /recommend handler
        # (app/routers/v1/recommend.py) — exact match first, then a
        # case-insensitive scan, then a clear 400 rather than letting
        # RecommendationRequest's validator raise on a None family.
        crops_res = await crop_manager.fetch_all(limit=1, filters={"name_en": rotation_crop_name})
        if crops_res.items:
            previous_crop_family = crops_res.items[0].family
        else:
            all_crops = await crop_manager.fetch_all(limit=0)
            matched = next((c for c in all_crops.items if c.name_en.lower() == rotation_crop_name.lower()), None)
            if not matched:
                raise HTTPException(status_code=400, detail=f"previous_crop '{rotation_crop_name}' not found in crop database")
            previous_crop_family = matched.family

    rec_request = RecommendationRequest(
        lat=location[1] if location else None,
        lon=location[0] if location else None,
        pincode=answers.pincode if location is None else None,
        season=season, water_methods=irrigation, budget_bucket=budget_bucket,
        previous_crop_family=previous_crop_family, was_fallow=was_fallow, top_n=5,
    )

    recommendation_id = str(uuid.uuid4())

    # DEF-02: meter usage against the same subscription quota /api/v1/recommend
    # already enforces — verify before the (paid) scoring pipeline runs.
    verify_ok, _ = await subscription_service.verify(upid, payload.subscription_id)
    if not verify_ok:
        raise HTTPException(status_code=480, detail="limit exceeded, subscribe now")

    try:
        response = await scoring_engine.recommend(rec_request, auto_detect_soil=True)
    except Exception:
        await subscription_service.rollback(upid, payload.subscription_id)
        raise

    # Usage consumed successfully -- confirm and log history. Both are
    # best-effort: a subscription-service hiccup or a history-write failure
    # shouldn't turn an already-generated recommendation into an error.
    confirm_ok, _ = await subscription_service.confirm(upid, payload.subscription_id)
    if not confirm_ok:
        logger.error(f"subscription confirm failed for upid={upid} subscription_id={payload.subscription_id}")

    try:
        await history_manager.create(RecommendationHistorySchema(
            upid=upid,
            subscription_id=payload.subscription_id,
            lat=rec_request.lat,
            lon=rec_request.lon,
            season=season,
            water_methods=irrigation,
            budget_bucket=budget_bucket,
            previous_crop_code=rotation_crop_name,
            was_fallow=was_fallow,
            top_n=5,
            lang="en",
            zone_filter_applied=response.zone_filter_applied,
            soil_class_detected=response.soil_class_detected,
            soil_texture_label=response.soil_texture_label,
            results=[r.model_dump() for r in response.results],
            notes=response.notes,
            farm_id=payload.farmId,
            farm_name=answers.farm_name or (farm.get("name") if farm else None),
            livestock=answers.livestock,
            current_crop_code=rotation_crop_name,
            sowing_date=answers.sowing_date,
        ))
    except Exception as exc:
        logger.error(f"failed to write wizard recommendation history for upid={upid}: {exc}")

    try:
        if response.results:
            top = response.results[0]
            await user_crop_report_manager.create(UserCropReportSchema(
                user_id=upid,
                farm_id=payload.farmId,
                recommendation_id=recommendation_id,
                farm_name=answers.farm_name or (farm.get("name") if farm else None),
                season=season,
                language="en",
                top_crop_id=top.crop.lower().replace(" ", "_"),
                top_crop_name=top.crop,
                top_crop_score=round(top.suitability_pct),
                total_recommendations=len(response.results),
                report_data={"results": [r.model_dump() for r in response.results]},
            ))
    except Exception as exc:
        logger.error(f"failed to pre-hydrate user_crop_reports for upid={upid}: {exc}")

    recommendations = [
        WizardRecommendationItem(
            id=f"rec_{i}", crop_name=r.crop, suitability_score=round(r.suitability_pct),
            roi_estimate="High" if r.suitability_pct >= 80 else "Medium" if r.suitability_pct >= 50 else "Low",
            duration_days=None, water_requirement=None,
            reason=f"Suitability {r.suitability_pct:.0f}% for {season.title()} season.",
        )
        for i, r in enumerate(response.results)
    ]

    # Awaited, not fire-and-forget: EventBrokerService.publish already
    # guarantees a Kafka outage never raises into this response (Review
    # Focus) -- it falls back to the event_outbox internally; awaiting here
    # (instead of asyncio.create_task) keeps the call deterministic for
    # tests and avoids a task that can outlive the request's own lifecycle.
    await _publish_crop_guide_completed(upid, payload, farm, season, budget_bucket, recommendation_id)

    return WizardSubmitResponse(recommendations=recommendations, recommendation_id=recommendation_id)


async def _publish_crop_guide_completed(
    upid: str, payload: WizardSubmitRequest, farm: dict | None, season: str, budget_bucket: str,
    recommendation_id: str,
) -> None:
    """Awaited by the caller; EventBrokerService.publish never raises (it
    falls back to the event_outbox on a Kafka outage), so a broker failure
    never turns a working recommendation into a 500."""
    answers = payload.answers
    event = {
        "event_id": recommendation_id,
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
            "wizard_session_id": payload.wizard_session_id,
        },
    }
    await event_broker.publish("crop_guide_completed", event, key=payload.farmId or event["event_id"])
