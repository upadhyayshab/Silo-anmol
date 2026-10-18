import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from config import get_settings, get_engine
from dependencies.auth import require_jwt
from managers import CropManager, RecommendationHistoryManager, RecommendationHistorySchema
from managers.cropReportManagers import CropReportManager
from models import (
    PublicRecommendationRequest,
    RecommendationRequest,
    RecommendationResponse,
)
from services import SubscriptionService
from services.subscriptionService import paywall_if_refused
from services.cropCatalog import CropCatalog
from utils.farmerText import note, season_name
from utils.languages import normalize_lang
from utils.scoringEngine import CropScoringEngine

logger = logging.getLogger("recommend_router")

router = APIRouter(prefix="/recommend")
settings = get_settings()
engine = get_engine(settings.name)
crop_manager = CropManager(engine)
scoring_engine = CropScoringEngine(engine)
subscription_service = SubscriptionService()
history_manager = RecommendationHistoryManager(engine)
crop_catalog = CropCatalog(CropReportManager(engine), crop_manager)


@router.post("", response_model=RecommendationResponse)
async def post_recommend(
    payload: PublicRecommendationRequest,
    lat: float = Query(..., ge=-90.0, le=90.0, description="Latitude of the farm location"),
    lon: float = Query(..., ge=-180.0, le=180.0, description="Longitude of the farm location"),
    lang: str = Query("en", description="Language code for translated crop names (e.g. 'en', 'hi', 'kn')"),
    auth_payload: dict = Depends(require_jwt),
):
    """
    Get customized crop recommendations based on location, season, irrigation, budget, and crop rotation.

    - **Authentication**: Requires valid `x-access-jwt` header with `upid`.
    - **Subscription**: Requires a `subscription_id` with remaining usage; usage is verified before
      scoring and confirmed only after crops are returned (a failed or empty run uses no credit).
    - **Location**: `lat` & `lon` automatically resolve Agro-Climatic Zone and USDA Soil Texture.
    - **Language**: Translates crop names to requested language if available (`lang=hi`, `kn`, `ta`, etc.;
      the legacy Hindi code `hn` and locale forms like `hi-IN` are also accepted; any other code,
      including the former `ur`/`ne`, gets English).
    - **Previous Crop**: Provide `previous_crop_code` (e.g. 'Wheat') or set `was_fallow=true` if land was barren.
    """
    upid = auth_payload["upid"]

    # English for codes outside the 12 product languages, like every other
    # endpoint: old apps may still send ur/ne, which this used to accept.
    lang_code = normalize_lang(lang)

    previous_crop_family: Optional[str] = None
    if not payload.was_fallow:
        if not payload.previous_crop_code:
            raise HTTPException(
                status_code=400,
                detail="previous_crop_code is required when was_fallow is false",
            )
        # Any spelling, Hindi/Marathi name or common alias of a catalog crop;
        # v1's contract still rejects a crop it can't place.
        previous_crop_family = await crop_catalog.find_family(payload.previous_crop_code)
        if not previous_crop_family:
            raise HTTPException(
                status_code=400,
                detail=f"Previous crop '{payload.previous_crop_code}' not found in database. Use /api/v1/options to get valid crop names.",
            )

    # Construct internal recommendation request
    internal_request = RecommendationRequest(
        lat=lat,
        lon=lon,
        season=payload.season,
        water_methods=payload.water_methods,
        soil_class=0,  # Will be auto-detected via auto_detect_soil=True
        budget_bucket=payload.budget_bucket,
        previous_crop_family=previous_crop_family,
        was_fallow=payload.was_fallow,
        top_n=payload.top_n,
    )

    # ── Subscription: the farmer's own, not-cancelled subscription with a
    # credit left, checked before running the (paid) scoring pipeline ──
    await subscription_service.check_quota(upid, payload.subscription_id)

    # Run recommendation with soil auto-detection and language translation
    # verify reserves nothing; only confirm (below, on success) counts the
    # credit, so a failed or empty run has nothing to roll back.
    response = await scoring_engine.recommend(
        internal_request,
        lang=lang_code,
        auto_detect_soil=True,
    )

    if not response.results:
        # No crop passed the zone/season/water filters: an empty answer must
        # not cost a credit, so it's never confirmed (same rule as the v2 wizard).
        season = getattr(payload.season, "value", payload.season)
        response.notes.append(note("no_crop_fits", lang_code, season=season_name(season, lang_code)))
        return response

    # Count the credit before saving; refused (another phone used the last
    # one) -> 480 instead of an unpaid result. The history write below is
    # best-effort.
    confirmed, confirm_data = await subscription_service.confirm(upid, payload.subscription_id)
    paywall_if_refused(confirmed, confirm_data, upid, payload.subscription_id)

    try:
        await history_manager.create(RecommendationHistorySchema(
            upid=upid,
            subscription_id=payload.subscription_id,
            lat=lat,
            lon=lon,
            season=payload.season,
            water_methods=[m.value for m in payload.water_methods],
            budget_bucket=payload.budget_bucket,
            previous_crop_code=payload.previous_crop_code,
            was_fallow=payload.was_fallow,
            top_n=payload.top_n,
            lang=lang_code,
            zone_filter_applied=response.zone_filter_applied,
            soil_class_detected=response.soil_class_detected,
            soil_texture_label=response.soil_texture_label,
            results=[r.model_dump() for r in response.results],
            notes=response.notes,
        ))
    except Exception as exc:
        logger.error(f"failed to write recommendation history for upid={upid}: {exc}")

    return response
