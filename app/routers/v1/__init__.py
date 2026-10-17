from fastapi import APIRouter

from config import get_settings, get_engine

from models import *

from .soil import router as soil_router
from .options import router as options_router
from .recommendations import router as recommendations_router
from .recommend import router as recommend_router
from .crops import router as crops_router

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter()
# The template /examples route wrote rows to the DB with no auth; it is not mounted.
router.include_router(soil_router)
router.include_router(options_router)
router.include_router(recommendations_router)
router.include_router(recommend_router)
router.include_router(crops_router)


@router.get("/health-check", response_model=HealthCheckResponse)
async def health_check():
    return HealthCheckResponse(
        status=f"{settings.name}[node:{0}/{0}] is running with {0}/{0} passing",
        version=settings.version,
        commit=settings.commit,
        branch=settings.branch,
        build_time=settings.build_time,
        build_number=settings.build_number,
        build_tags=settings.build_tags,
    )
