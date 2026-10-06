"""
Manure report endpoint — mounted at the app root.

  - POST /manure/report — quality-gates a cow-dung photo, scores it against
    manure_kb, stores the report in manure_reports_v1 and returns the
    farmer-facing report body. 422 when the photo is not manure or not fresh.
"""
from typing import Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from models.manureReportModels import ManureReportResponse
from routers.manure import _resolve_image
from services.manureReportService import ManureReportService

router = APIRouter()
manure_report_service = ManureReportService()

# Subscription check is not wired yet: every caller is treated as subscribed.
DEFAULT_HAS_SUBS = True


@router.post(
    "/manure/report",
    response_model=ManureReportResponse,
    responses={422: {"description": "Image is not cow dung (NOT_MANURE) or not fresh (NOT_FRESH)"}},
)
async def manure_report(
    lang_code: str = Form("en"),
    category: str = Form("manure"),
    cow_id: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    image_url: Optional[str] = Form(None),
    x_upid: str = Header(..., alias="x-upid"),
    subs_id: Optional[str] = Header(None, alias="x-subscription-id"),
):
    if category != "manure":
        raise HTTPException(status_code=400, detail="category must be 'manure'")
    cow_id = (cow_id or "").strip() or None

    image_bytes, mime_type = await _resolve_image(image, image_url)
    # Local file name for now; replaced by the S3 URL once uploads are wired.
    img_url = (image_url or "").strip() or image.filename

    try:
        report = await manure_report_service.create_report(
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
        raise HTTPException(status_code=502, detail=f"Manure report failed: {e}")

    # Returned directly (not via response_model) so cow_id stays absent rather
    # than null when it was not sent; the model still documents the shape.
    return JSONResponse(content=jsonable_encoder(ManureReportResponse(**report), exclude_unset=True))


__all__ = ["router"]
