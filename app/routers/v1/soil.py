from fastapi import APIRouter, Query

from models import SoilTextureResponse
from utils.soilTexture import get_soil_texture_lookup

router = APIRouter(prefix="/soil-texture")

SUPPORTED_LANGS = {"en"}


@router.get("", response_model=SoilTextureResponse)
async def get_soil_texture(
        lat: float = Query(..., ge=-90, le=90),
        lon: float = Query(..., ge=-180, le=180),
        lang: str = Query("en"),
):
    lang = lang if lang in SUPPORTED_LANGS else "en"
    lookup = get_soil_texture_lookup()
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
