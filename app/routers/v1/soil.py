import logging

from fastapi import APIRouter, HTTPException, Query

from models import SoilTextureResponse
from utils.soilTexture import get_soil_texture_lookup

router = APIRouter(prefix="/soil-texture")

SUPPORTED_LANGS = {"en"}
logger = logging.getLogger("soil_router")


@router.get("", response_model=SoilTextureResponse)
async def get_soil_texture(
        lat: float = Query(..., ge=-90, le=90),
        lon: float = Query(..., ge=-180, le=180),
        lang: str = Query("en"),
):
    lang = lang if lang in SUPPORTED_LANGS else "en"
    try:
        lookup = get_soil_texture_lookup()
    except Exception as exc:  # raster unreachable (S3 credentials, network)
        logger.error(f"soil texture raster unavailable: {exc}")
        raise HTTPException(status_code=503, detail="Soil data is temporarily unavailable. Please try again.")
    result = lookup.get_texture(lat=lat, lon=lon)
    return SoilTextureResponse(
        latitude=result.latitude,
        longitude=result.longitude,
        soil_class=result.soil_class,
        texture_class=result.texture_class,
        status=result.status,
        message=result.message,
        lang=lang,
    )
