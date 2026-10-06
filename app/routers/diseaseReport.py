"""
Disease report endpoint — mounted at the app root alongside /manure/report.

  - POST /disease/report — screens cattle image for KB diseases, saves the
    report to disease_reports_v1, and returns farmer/vet-facing report body.
    422 when the photo is not a cattle image.
"""
from typing import Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from models.diseaseReportModels import DiseaseReportResponse
from routers.manure import _resolve_image
from services.diseaseReportService import disease_report_service

router = APIRouter()

DEFAULT_HAS_SUBS = True


@router.post(
    "/disease/report",
    response_model=DiseaseReportResponse,
    responses={422: {"description": "Image is not a cattle photo (NOT_CATTLE)"}},
)
async def disease_report(
    lang_code: str = Form("en"),
    category: str = Form("disease"),
    cow_id: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    image_url: Optional[str] = Form(None),
    x_upid: str = Header(..., alias="x-upid"),
    subs_id: Optional[str] = Header(None, alias="x-subscription-id"),
):
    if category != "disease":
        raise HTTPException(status_code=400, detail="category must be 'disease'")
    cow_id = (cow_id or "").strip() or None

    image_bytes, mime_type = await _resolve_image(image, image_url)
    img_url = (image_url or "").strip() or (image.filename if image else "upload:image")

    try:
        report = await disease_report_service.create_report(
            image_bytes=image_bytes,
            mime_type=mime_type,
            img_url=img_url,
            lang_code=lang_code,
            user_id=x_upid,
            cow_id=cow_id,
            subs_id=subs_id,
            has_subs=DEFAULT_HAS_SUBS,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Disease report failed: {e}")

    return JSONResponse(content=jsonable_encoder(DiseaseReportResponse(**report), exclude_unset=True))


__all__ = ["router"]
