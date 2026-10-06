"""
Cow-dung endpoints — mounted at the app root (no /api/v1 prefix) so
/detect/manure is byte-identical to vlm-detectors' path. Once this service is
deployed, gau-swasth-backend-service's VLM_BASE_URL can point here unchanged.

Two independent endpoints:
  - POST /detect/manure               — the plain 1-5 consistency score.
  - POST /detect/manure/subcategory   — the colour/condition sub-code, from
                                         the 27-way master_table_v2 taxonomy.

Plus one compatibility shim:
  - POST /chat/manure                 — same request/response shape as
    vlm-detectors' /chat/manure (JSON body, {response, detections}), so
    gau-swasth-backend-service's existing send_manure_message works against
    this service unmodified. NOT a tool-calling chat loop — one async Gemini
    call via ManureSubcategoryService, same as the other two endpoints.
"""
import mimetypes
from typing import Optional

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from models import ManureScoreResult, ManureSubcategoryResult
from services import ManureScoreService, ManureSubcategoryService

router = APIRouter()
manure_score_service = ManureScoreService()
manure_subcategory_service = ManureSubcategoryService()


async def _resolve_image(image: Optional[UploadFile], image_url: Optional[str]) -> tuple[bytes, str]:
    # Form clients (curl -F, Swagger "try it out") send empty-string/empty-file
    # fields for the one you didn't fill in, not an omitted field — normalize
    # those to None before the exactly-one-of check.
    image_url = image_url.strip() or None if image_url is not None else None
    if image is not None and not image.filename:
        image = None

    if (image is None and image_url is None) or (image is not None and image_url is not None):
        raise HTTPException(
            status_code=400,
            detail="Provide either 'image' file or 'image_url', not both or neither",
        )

    if image is not None:
        mime_type = image.content_type or mimetypes.guess_type(image.filename or "")[0] or "image/jpeg"
        return await image.read(), mime_type

    async with httpx.AsyncClient() as client:
        resp = await client.get(image_url, timeout=30)
        resp.raise_for_status()
        mime_type = resp.headers.get("content-type") or mimetypes.guess_type(image_url)[0] or "image/jpeg"
        return resp.content, mime_type.split(";")[0]


@router.post("/detect/manure", response_model=ManureScoreResult)
async def detect_manure(
    lang_code: str = Form(...),
    image: Optional[UploadFile] = File(None),
    image_url: Optional[str] = Form(None),
) -> ManureScoreResult:
    """
    Score cow dung from an image. Single Gemini call, no chat/tool-calling.

    Same request shape as vlm-detectors' /detect/manure (lang_code + either
    image or image_url). Response is minimal for now (score, health_status,
    priority, reasoning) — the full farmer-facing report body per score is
    added once it's supplied.
    """
    image_bytes, mime_type = await _resolve_image(image, image_url)
    try:
        result = await manure_score_service.score(image_bytes, mime_type)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Manure scoring failed: {e}")
    result["image_url"] = image_url or f"upload:{image.filename}"
    return ManureScoreResult(**result)


@router.post("/detect/manure/subcategory", response_model=ManureSubcategoryResult)
async def detect_manure_subcategory(
    lang_code: str = Form(...),
    image: Optional[UploadFile] = File(None),
    image_url: Optional[str] = Form(None),
) -> ManureSubcategoryResult:
    """
    Classify cow dung against the 27-way colour/condition sub-code taxonomy
    (data/manure_master_table.json). Single Gemini call, independent of
    /detect/manure — determines its own score (each sub-code carries a Score
    column) rather than requiring one to be passed in.

    Every field but `reasoning` is a deterministic lookup on the matched
    table row, including `message`, which is built by one shared composer
    (build_common_message) so every sub-code's farmer-facing text is
    generated the same way from the table's own advice/severity columns.
    """
    image_bytes, mime_type = await _resolve_image(image, image_url)
    try:
        result = await manure_subcategory_service.classify(image_bytes, mime_type)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Manure sub-category classification failed: {e}")
    result["image_url"] = image_url or f"upload:{image.filename}"
    return ManureSubcategoryResult(**result)


class ChatManureRequest(BaseModel):
    messages: list[dict]
    lang_code: str = "en"
    max_tool_calls: int = 10


def _latest_image_url(messages: list[dict]) -> Optional[str]:
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    url = (block.get("image_url") or {}).get("url")
                    if url:
                        return url
    return None


@router.post("/chat/manure")
async def chat_manure(payload: ChatManureRequest) -> dict:
    """
    Compatibility shim for gau-swasth-backend-service's LLMService.manure_chat_completion,
    which POSTs {messages, lang_code, max_tool_calls} and expects back
    {response|content, title?, detections?}. One async sub-category
    classification call — no tool-calling loop.
    """
    image_url = _latest_image_url(payload.messages)
    if not image_url:
        raise HTTPException(status_code=400, detail="No image found in messages")

    async with httpx.AsyncClient() as client:
        resp = await client.get(image_url, timeout=30)
        resp.raise_for_status()
        mime_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
        image_bytes = resp.content

    try:
        result = await manure_subcategory_service.classify(image_bytes, mime_type)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Manure classification failed: {e}")

    result["image_url"] = image_url

    return {
        "response": result["message"],
        "image_url": image_url,
        "detections": [result],
    }


__all__ = ["router"]
