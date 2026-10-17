"""Crop guide report/dossier endpoints — spec Section 5 Phase 5, mounted at
/api/v2 rather than the spec's literal /api/v6 (this plan's versioning
decision). GAP-06: routes live at their clean REST paths (no router-level
"/reports" prefix, which previously doubled up with each route's own
trailing "/reports" segment); the old doubled paths stay registered as
hidden aliases for any client still calling them."""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from config import get_engine, get_settings
from dependencies.auth import require_jwt
from managers.cropManagers import CropManager
from managers.cropReportManagers import CropReportManager, UserCropReportManager
from models.reportModels import SingleCropDossier, UnifiedCropReportResponse, UserReportsListResponse
from services.cropReportService import CropReportService

router = APIRouter(tags=["Crop Guide Reports v2"])
settings = get_settings()
engine = get_engine(settings.name)
report_service = CropReportService(
    crop_report_manager=CropReportManager(engine),
    user_crop_report_manager=UserCropReportManager(engine),
    crop_manager=CropManager(engine),
)


@router.get("/users/{user_id}/reports", response_model=UserReportsListResponse)
@router.get(
    "/reports/users/{user_id}/reports", response_model=UserReportsListResponse, include_in_schema=False,
)
async def list_user_reports(
    user_id: str = Path(...),
    farm_id: Optional[str] = Query(None),
    farmId: Annotated[Optional[str], Query(include_in_schema=False)] = None,  # spelling the integration spec uses
    recommendation_id: Optional[str] = Query(None),
    lang: str = Query("en"),
    auth_payload: dict = Depends(require_jwt),
):
    if user_id != auth_payload.get("upid"):
        raise HTTPException(status_code=403, detail="Forbidden")
    return await report_service.list_user_reports(
        user_id=user_id, farm_id=farm_id or farmId, recommendation_id=recommendation_id, lang=lang,
    )


@router.get("/recommendations/{recommendation_id}/report", response_model=UnifiedCropReportResponse)
@router.get(
    "/reports/recommendations/{recommendation_id}/report",
    response_model=UnifiedCropReportResponse, include_in_schema=False,
)
async def get_recommendation_report(
    recommendation_id: str = Path(...),
    lang: str = Query("en"),
    crop_id: Optional[str] = Query(None),
    farm_id: Optional[str] = Query(None),
    auth_payload: dict = Depends(require_jwt),
):
    try:
        return await report_service.get_or_create_unified_report(
            recommendation_id=recommendation_id, user_id=auth_payload.get("upid"),
            farm_id=farm_id, lang=lang, focus_crop_id=crop_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/recommendations/{recommendation_id}/reports/{crop_id}", response_model=UnifiedCropReportResponse)
async def get_recommendation_crop_report(
    recommendation_id: str = Path(...),
    crop_id: str = Path(..., description="crop_id from the submit response (names are also accepted)"),
    lang: str = Query("en"),
    farm_id: Optional[str] = Query(None),
    auth_payload: dict = Depends(require_jwt),
):
    """Integration spec's per-crop dossier route: the same unified report,
    with `crop_id` shown first. 404 if that crop isn't in the run."""
    try:
        return await report_service.get_or_create_unified_report(
            recommendation_id=recommendation_id, user_id=auth_payload.get("upid"),
            farm_id=farm_id, lang=lang, focus_crop_id=crop_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/crops/{crop_id}/report", response_model=SingleCropDossier)
@router.get(
    "/reports/crops/{crop_id}/report", response_model=SingleCropDossier, include_in_schema=False,
)
async def get_master_crop_report(
    crop_id: str = Path(...),
    lang: str = Query("en"),
    auth_payload: dict = Depends(require_jwt),
):
    try:
        return await report_service.get_master_report(crop_id=crop_id, lang=lang)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


__all__ = ["router"]
