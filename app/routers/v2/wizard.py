from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query

from config import get_engine, get_settings
from dependencies.auth import require_jwt
from managers.cropManagers import CropManager, RecommendationHistoryManager, RecommendationHistorySchema
from managers.cropReportManagers import CropReportManager, UserCropReportManager, UserCropReportSchema
from models.wizardModels import (
    FarmContext, WizardQuestion, WizardQuestionOption, WizardQuestionsResponse,
)
from services.agroInternalService import AgroInternalService, AgroUnavailableError
from services.cropCatalog import CropCatalog, crop_slug
from utils.languages import normalize_lang
from utils.matchReasons import match_reasons
from utils.wizardTranslations import OPTION_LABELS, QUESTION_LABELS

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import text

from models.recommendationModels import RecommendationRequest
from models.wizardModels import WizardContext, WizardRecommendationItem, WizardSubmitRequest, WizardSubmitResponse
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
crop_report_manager = CropReportManager(engine)

_catalog: Optional[CropCatalog] = None


def _get_catalog() -> CropCatalog:
    """One cached catalog per process, rebuilt only if the module's managers
    are swapped (tests patch crop_manager / crop_report_manager)."""
    global _catalog
    if _catalog is None or _catalog.crop_manager is not crop_manager \
            or _catalog.crop_report_manager is not crop_report_manager:
        _catalog = CropCatalog(crop_report_manager, crop_manager)
    return _catalog


ALL_QUESTIONS: list[WizardQuestion] = [
    WizardQuestion(id="location", type="map_pin", label="Where is your farm?", required=True),
    WizardQuestion(
        id="season", type="radio", label="Which season are you planning to sow for?",
        options=[
            WizardQuestionOption(label="Kharif (Monsoon: Jun - Oct)", value="Kharif"),
            WizardQuestionOption(label="Rabi (Winter: Oct - Mar)", value="Rabi"),
            WizardQuestionOption(label="Summer / Zaid (Mar - Jun)", value="Summer"),
            WizardQuestionOption(label="Long-term (Perennial / Annual)", value="Annual"),
        ],
    ),
    WizardQuestion(
        id="has_active_crop", type="radio", label="Do you currently have a crop growing?",
        options=[WizardQuestionOption(label="Yes", value="Yes"), WizardQuestionOption(label="No", value="No")],
    ),
    WizardQuestion(
        id="crop", type="search", label="Which crop is growing?", required=False,
        search_endpoint="/api/v6/crops",
        depends_on={"field": "has_active_crop", "value": "Yes"},
    ),
    WizardQuestion(
        id="sowing_date", type="date", label="When did you sow?", required=False,
        depends_on={"field": "has_active_crop", "value": "Yes"},
    ),
    # Asked on both branches, as in the Crop Guide flowchart: a farmer growing
    # a crop now is also asked what grew before it.
    WizardQuestion(
        id="previous_crop", type="search", label="What was grown here before?", required=False,
        search_endpoint="/api/v6/crops",
    ),
    WizardQuestion(
        id="irrigation", type="multi_select", label="How do you water this field?",
        options=[
            WizardQuestionOption(label="Rainfed", value="Rainfed"),
            WizardQuestionOption(label="Flood (canal / furrow)", value="Flood"),
            WizardQuestionOption(label="Drip", value="Drip"),
            WizardQuestionOption(label="Sprinkler", value="Sprinkler"),
        ],
    ),
    # Labels stay as the scoring engine's questionnaire (v3) defines B1-B4
    # (decision D3); the flowchart and specs are updated to match.
    WizardQuestion(
        id="budget", type="radio", label="How much can you spend per acre next season?",
        options=[
            WizardQuestionOption(label="<₹10k", value="<₹10k"),
            WizardQuestionOption(label="₹10–25k", value="₹10–25k"),
            WizardQuestionOption(label="₹25–50k", value="₹25–50k"),
            WizardQuestionOption(label=">₹50k", value=">₹50k"),
        ],
    ),
    WizardQuestion(
        id="livestock", type="radio", label="What type of farm do you have?",
        options=[
            WizardQuestionOption(label="Cattle farm - Cows & Buffaloes", value="Cattle farm - Cows & Buffaloes"),
            WizardQuestionOption(label="Cattle farm - Goat, Sheep, & Pig", value="Cattle farm - Goat, Sheep, & Pig"),
            WizardQuestionOption(label="Poultry farm", value="Poultry farm"),
            WizardQuestionOption(label="Fisheries", value="Fisheries"),
            WizardQuestionOption(label="I don't have any livestock", value="I don't have any live stock"),
        ],
    ),
    WizardQuestion(id="farm_name", type="text", label="Give your farm a name", required=False),
]

# Crop Guide flowchart, Has Farm Path: once a farm is selected, the farm
# itself answers the crop questions and its name, so only season, budget and
# livestock remain -- plus location or watering if the farm lacks them,
# because scoring can't run without either.
FARM_ANSWERED_QUESTIONS = {"farm_name", "has_active_crop", "crop", "sowing_date", "previous_crop"}


def _localized(questions: list[WizardQuestion], lang: str) -> list[WizardQuestion]:
    labels = QUESTION_LABELS.get(lang)
    if not labels:
        return questions
    option_labels = OPTION_LABELS.get(lang, {})
    localized = []
    for question in questions:
        options = question.options
        if options:
            per_question = option_labels.get(question.id, {})
            options = [o.model_copy(update={"label": per_question.get(o.value, o.label)}) for o in options]
        localized.append(question.model_copy(update={"label": labels.get(question.id, question.label), "options": options}))
    return localized


@router.get("/wizard/questions", response_model=WizardQuestionsResponse)
async def get_wizard_questions(
    farmId: Optional[str] = Query(None),
    lang: Annotated[str, Query(description="ISO 639-1 language code for labels; values stay English")] = "en",
    auth_payload: dict = Depends(require_jwt),
):
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
            suppressed_ids.update(FARM_ANSWERED_QUESTIONS)
            if farm.get("location") or farm.get("center"):
                suppressed_ids.add("location")
            if _map_irrigation(farm.get("irrigation") or farm.get("irrigation_methods") or [], strict=False):
                suppressed_ids.add("irrigation")

    questions = [q for q in ALL_QUESTIONS if q.id not in suppressed_ids]
    return WizardQuestionsResponse(farm_context=farm_context, questions=_localized(questions, normalize_lang(lang)))


_SEASON_MAP = {
    "kharif": "KHARIF", "rabi": "RABI", "summer": "ZAID", "zaid": "ZAID",
    "annual": "LONGTERM", "long-term": "LONGTERM", "longterm": "LONGTERM", "long term": "LONGTERM",
    "perennial": "LONGTERM",
}

_BUDGET_MAP = {
    # Flowchart / TRD brackets (standard values and shorthands)
    "20k-40k": "B1", "40k-70k": "B2", "70k-1l": "B3", "1l-1.2l": "B4",
    "70k-1lakh": "B3", "1lakh-1.2lakh": "B4", "70k-100k": "B3", "100k-120k": "B4",
    # Full numeric range values without k/l shorthand
    "20000-40000": "B1", "40000-70000": "B2", "70000-100000": "B3", "100000-120000": "B4",
    # Questionnaire (v3) formats
    "<10k": "B1", "<10000": "B1", "10-25k": "B2", "10k-25k": "B2",
    "25-50k": "B3", "25k-50k": "B3", ">50k": "B4", ">50000": "B4",
    # Direct canonical codes
    "b1": "B1", "b2": "B2", "b3": "B3", "b4": "B4",
    # Backwards compatibility with the earlier 3-tier wizard options
    "low": "B1", "medium": "B3", "high": "B4",
}

# Every irrigation spelling the two services and their docs use -> engine
# enum. "Flood" (spec, Agro) and "Surface" (engine) are the same method.
# "canal" is Agro's old name for flood watering (Agro now stores it as Flood).
_IRRIGATION_MAP = {
    "rainfed": "RAINFED", "surface": "SURFACE", "flood": "SURFACE", "canal": "SURFACE",
    "drip": "DRIP", "sprinkler": "SPRINKLER",
}

# Engine enum -> the canonical title-case name Agro stores and its
# irrigation picker shows (GET /api/v6/irrigation-methods).
_EVENT_IRRIGATION = {"RAINFED": "Rainfed", "SURFACE": "Flood", "DRIP": "Drip", "SPRINKLER": "Sprinkler"}


def _map_season(season: str) -> str:
    mapped = _SEASON_MAP.get(season.strip().lower())
    if mapped is None:
        raise ValueError(f"Unrecognized season '{season}'. Expected one of: Kharif, Rabi, Summer, Long-term")
    return mapped


def _map_budget(budget: str) -> str:
    cleaned = (
        budget.strip().lower().replace(" ", "")
        .replace("₹", "").replace(",", "").replace("/acre", "")
        .replace("–", "-").replace("—", "-")  # unicode en-dash / em-dash
    )
    mapped = _BUDGET_MAP.get(cleaned)
    if mapped is None:
        raise ValueError(
            f"Unrecognized budget '{budget}'. Expected one of: <₹10k, ₹10–25k, ₹25–50k, >₹50k "
            "(or B1-B4, 20K-40K, etc.)"
        )
    return mapped


def _parse_sowing_date(raw: Optional[str]) -> Optional[date]:
    """RecommendationHistorySchema.sowing_date is a real db.Date() column --
    SQLAlchemy does not coerce strings into Python date objects, so passing
    the wizard's raw ISO string through raises a TypeError at write time (the
    bug this guards against). A malformed value must not lose the whole
    audit row, so it's logged and dropped rather than raised."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError) as exc:
        logger.warning(f"failed to parse sowing_date '{raw}': {exc}")
        return None


def _map_irrigation(methods: list[str], strict: bool = True) -> list[str]:
    """strict (the farmer's own answers): an unknown method is an error.
    Not strict (values inherited from the farm record): unknown values are
    dropped, so an old farm's odd value can't block a farmer who was never
    asked the question."""
    if isinstance(methods, str):
        methods = [methods]
    mapped: list[str] = []
    unknown = []
    for method in methods:
        code = _IRRIGATION_MAP.get(str(method).strip().lower())
        if code is None:
            unknown.append(method)
        elif code not in mapped:
            mapped.append(code)
    if unknown and strict:
        raise ValueError(
            f"Unrecognized irrigation method(s) {unknown}. Expected one of: Rainfed, Flood, Drip, Sprinkler"
        )
    return mapped


def _session_event_id(upid: str, wizard_session_id: Optional[str], recommendation_id: str) -> str:
    """Same wizard session -> same event id, so Agro's processed_events dedupe
    also catches a resubmission that slipped past the replay check."""
    if not wizard_session_id:
        return recommendation_id
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"crop_guide_completed:{upid}:{wizard_session_id}"))


@router.post("/wizard/submit", response_model=WizardSubmitResponse)
async def submit_wizard(
    payload: WizardSubmitRequest,
    lang: str = Query("en", description="ISO 639-1 language code"),
    auth_payload: dict = Depends(require_jwt),
):
    """TRD Section 8.6/Section 8.7: synchronous ML recommendation + async CropGuideCompleted
    emission. The Kafka publish never blocks or fails the HTTP response
    (Review Focus) — it's fired and its result is only logged."""
    lang_code = normalize_lang(lang)
    upid = auth_payload["upid"]
    if not payload.wizard_session_id:
        return await _submit(payload, lang_code, upid)

    # A retried or double-tapped submit (same wizard_session_id) gets the run
    # it already paid for: no second charge, no second farm. The lock makes a
    # tap that arrives while the first is still running wait for it and then
    # replay its result, instead of also passing the check and paying again.
    async with _session_lock(f"wizard_submit:{upid}:{payload.wizard_session_id}"):
        previous = await user_crop_report_manager.fetch_all(
            limit=1, filters={"user_id": upid, "wizard_session_id": payload.wizard_session_id},
        )
        if previous.items and (previous.items[0].report_data or {}).get("response"):
            return WizardSubmitResponse(**previous.items[0].report_data["response"])
        return await _submit(payload, lang_code, upid)


@asynccontextmanager
async def _session_lock(key: str):
    """Postgres advisory lock for `key`, held until the block exits (the
    transaction ends, releasing it, even if the block raises). Other
    databases (tests) run the block unlocked."""
    if engine.dialect.name != "postgresql":
        yield
        return
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
        yield


async def _submit(payload: WizardSubmitRequest, lang_code: str, upid: str) -> WizardSubmitResponse:
    answers = payload.answers
    catalog = _get_catalog()

    farm = None
    if payload.farmId:
        try:
            farm = await agro_internal_service.get_farm(payload.farmId, user_id=upid, raise_if_unavailable=True)
        except AgroUnavailableError:
            raise HTTPException(status_code=503, detail="Farm service is temporarily unavailable. Please try again.")
        if farm is None:
            raise HTTPException(status_code=404, detail=f"Farm '{payload.farmId}' not found")

    location = answers.location
    if location is None and farm:
        location = farm.get("location") or farm.get("center")
    # The farmer's PIN code, else the farm's: the scoring engine falls back to
    # it whenever the coordinates can't be reverse-geocoded.
    pincode = answers.pincode or (farm.get("pincode") if farm else None)
    if location is None and not pincode:
        raise HTTPException(status_code=400, detail="answers.location is required when farmId is not provided or the farm has no boundary yet")

    try:
        season = _map_season(answers.season)
        budget_bucket = _map_budget(answers.budget) if answers.budget else "B3"
        if answers.irrigation:
            irrigation = _map_irrigation(answers.irrigation)
        elif farm:
            irrigation = _map_irrigation(farm.get("irrigation") or farm.get("irrigation_methods") or [], strict=False)
        else:
            irrigation = []
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not irrigation:
        raise HTTPException(status_code=400, detail="answers.irrigation is required (at least one method)")

    # Rotation predecessor = what is in the soil now. DEF-04: a farm's active
    # standing crop wins; on the no-farm path the crop the farmer says is
    # growing now comes next; only then the crop grown before. Never written
    # back to the farm — this only feeds the scoring engine's rotation input.
    answered_active_crop = answers.crop if (answers.has_active_crop or "").strip().lower() == "yes" else None
    if farm and farm.get("has_active_crop") and farm.get("crop"):
        rotation_crop_name = farm["crop"]
    elif answered_active_crop:
        rotation_crop_name = answered_active_crop
    else:
        rotation_crop_name = answers.previous_crop or (farm.get("previous_crop") if farm else None)
    was_fallow = rotation_crop_name is None

    # Agro's crop list is far larger than the 104 scored crops, so a name the
    # catalog can't place drops the rotation factor instead of failing.
    previous_crop_family = None
    rotation_unknown = False
    if not was_fallow:
        scoring_row = await catalog.find_scoring(rotation_crop_name)
        if scoring_row is not None:
            previous_crop_family = scoring_row.family
        else:
            rotation_unknown = True
            logger.info(f"rotation crop '{rotation_crop_name}' not in the crop catalog; rotation factor skipped")

    rec_request = RecommendationRequest(
        lat=location[1] if location else None,
        lon=location[0] if location else None,
        pincode=pincode,
        season=season, water_methods=irrigation, budget_bucket=budget_bucket,
        previous_crop_family=previous_crop_family, was_fallow=was_fallow,
        rotation_unknown=rotation_unknown, top_n=5,
    )

    recommendation_id = str(uuid.uuid4())
    sowing_date = _parse_sowing_date(answers.sowing_date)
    # A new farmer's farm doesn't exist yet: its Agro uid is chosen here and
    # sent in the event, so this run is linked to the farm from the start.
    new_farm_uid = None if payload.farmId else f"farms_{uuid.uuid4()}"
    farm_id = payload.farmId or new_farm_uid

    # DEF-02: meter usage against the same subscription quota /api/v1/recommend
    # already enforces — checked before the (paid) scoring pipeline runs:
    # the farmer's own, not-cancelled subscription with a credit left.
    await subscription_service.check_quota(upid, payload.subscription_id)

    try:
        response = await scoring_engine.recommend(rec_request, lang=lang_code, auto_detect_soil=True)
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
            lang=lang_code,
            zone_filter_applied=response.zone_filter_applied,
            soil_class_detected=response.soil_class_detected,
            soil_texture_label=response.soil_texture_label,
            results=[r.model_dump() for r in response.results],
            notes=response.notes,
            farm_id=farm_id,
            farm_name=answers.farm_name or (farm.get("name") if farm else None),
            livestock=answers.livestock,
            # current_crop_code must describe what's actually standing on the
            # field right now, not the rotation predecessor used for scoring
            # (rotation_crop_name) -- those are different things when there's
            # no farm record and the farmer just reported a fresh active crop.
            current_crop_code=(
                farm.get("crop") if (farm and farm.get("has_active_crop"))
                else answered_active_crop
            ),
            sowing_date=sowing_date,
        ))
    except Exception as exc:
        logger.error(f"failed to write wizard recommendation history for upid={upid}: {exc}")

    recommendations = []
    top_crop_id = None
    for i, r in enumerate(response.results):
        master = await catalog.find_report(r.crop)
        crop_id = master.crop_id if master else crop_slug(r.crop)
        top_crop_id = top_crop_id or crop_id
        recommendations.append(WizardRecommendationItem(
            id=f"rec_{i}", crop_id=crop_id, crop_name=r.crop,
            crop_name_vernacular=(await catalog.local_name(master, lang_code) if master else None) or r.translated_name or r.crop,
            suitability_score=round(r.suitability_pct),
            is_top_pick=(i == 0),
            roi_estimate="High" if r.suitability_pct >= 80 else "Medium" if r.suitability_pct >= 50 else "Low",
            duration_days=master.duration_days if master else None,
            years_to_first_harvest=float(master.years_to_first_harvest) if master and master.years_to_first_harvest else None,
            water_requirement=master.water_requirement if master else None,
            reason=f"Suitability {r.suitability_pct:.0f}% for {season.title()} season.",
            match_reasons=match_reasons([f.model_dump() for f in r.factors], season, rotation_crop_name),
        ))

    result = WizardSubmitResponse(
        recommendations=recommendations, recommendation_id=recommendation_id, farm_id=farm_id, language=lang_code,
        context=WizardContext(
            zone_filter_applied=response.zone_filter_applied,
            soil_texture_label=response.soil_texture_label,
            notes=response.notes,
        ),
    )

    try:
        if response.results:
            top = response.results[0]
            await user_crop_report_manager.create(UserCropReportSchema(
                user_id=upid,
                farm_id=farm_id,
                recommendation_id=recommendation_id,
                wizard_session_id=payload.wizard_session_id,
                farm_name=answers.farm_name or (farm.get("name") if farm else None),
                season=season,
                language=lang_code,
                top_crop_id=top_crop_id,
                top_crop_name=top.crop,
                top_crop_score=round(top.suitability_pct),
                total_recommendations=len(response.results),
                report_data={
                    "results": [r.model_dump() for r in response.results],
                    "sowing_date": sowing_date.isoformat() if sowing_date else None,
                    "previous_crop": rotation_crop_name,
                    # Replayed verbatim for a resubmitted wizard_session_id.
                    "response": result.model_dump(),
                },
            ))
    except Exception as exc:
        logger.error(f"failed to pre-hydrate user_crop_reports for upid={upid}: {exc}")

    # Awaited, not fire-and-forget: EventBrokerService.publish already
    # guarantees a Kafka outage never raises into this response (Review
    # Focus) -- it falls back to the event_outbox internally; awaiting here
    # (instead of asyncio.create_task) keeps the call deterministic for
    # tests and avoids a task that can outlive the request's own lifecycle.
    await _publish_crop_guide_completed(
        upid, payload, season, budget_bucket, recommendation_id,
        irrigation=irrigation, location=location, pincode=pincode, sowing_date=sowing_date,
        new_farm_uid=new_farm_uid,
    )

    return result


async def _publish_crop_guide_completed(
    upid: str, payload: WizardSubmitRequest, season: str, budget_bucket: str,
    recommendation_id: str, *, irrigation: list[str], location: Optional[list], pincode: Optional[str],
    sowing_date: Optional[date], new_farm_uid: Optional[str],
) -> None:
    """Awaited by the caller; EventBrokerService.publish never raises (it
    falls back to the event_outbox on a Kafka outage), so a broker failure
    never turns a working recommendation into a 500.

    Sends checked values, not raw answers: irrigation in the title-case
    names Agro's picker uses, the validated [lon, lat], and an active crop
    only together with its name (Agro applies the same rules again)."""
    answers = payload.answers
    has_active_crop = (answers.has_active_crop or "").strip().lower() == "yes" and bool(answers.crop)
    event = {
        "event_id": _session_event_id(upid, payload.wizard_session_id, recommendation_id),
        "event_type": "CropGuideCompleted",
        "version": "1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": upid,
        "farm_id": payload.farmId,
        "data": {
            "farm_name": answers.farm_name,
            "center": location,
            "has_active_crop": has_active_crop,
            "crop": answers.crop if has_active_crop else None,
            "sowing_date": sowing_date.isoformat() if (has_active_crop and sowing_date) else None,
            "previous_crop": answers.previous_crop,
            "irrigation_methods": [_EVENT_IRRIGATION[m] for m in irrigation],
            "season": season,
            "budget": budget_bucket,
            "livestock": answers.livestock,
            "wizard_session_id": payload.wizard_session_id,
            "pincode": pincode,
            "recommendation_id": recommendation_id,
            "farm_uid": new_farm_uid,
        },
    }
    await event_broker.publish("crop_guide_completed", event, key=payload.farmId or new_farm_uid or event["event_id"])
