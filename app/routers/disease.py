"""
Disease screening endpoint — mounted at the app root alongside /detect/manure.

  - POST /detect/disease — screens one cattle photo for the KB diseases enabled
    in DiseaseService (currently D01-D04: bloat, TRP, lumpy jaw, listeriosis)
    in a single Gemini call.
"""
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from models import DiseaseDetectionResult
from routers.manure import _resolve_image
from services import DiseaseService

router = APIRouter()
disease_service = DiseaseService()


@router.post("/detect/disease", response_model=DiseaseDetectionResult)
async def detect_disease(
    lang_code: str = Form("en"),
    image: Optional[UploadFile] = File(None),
    image_url: Optional[str] = Form(None),
) -> DiseaseDetectionResult:
    """
    Screen a cattle image for each enabled KB disease. Same request shape as
    /detect/manure (lang_code + either image or image_url).

    Returns one finding per disease with a status of detected / suspected /
    not_detected / not_assessable. Farmer- and vet-facing text is looked up from
    the KB (English for now; lang_code is accepted for parity).
    """
    image_bytes, mime_type = await _resolve_image(image, image_url)
    try:
        result = await disease_service.detect(image_bytes, mime_type)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Disease screening failed: {e}")
    result["image_url"] = image_url or f"upload:{image.filename}"
    return DiseaseDetectionResult(**result)


__all__ = ["router"]
