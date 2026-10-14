from fastapi import APIRouter

from .wizard import router as wizard_router
from .reports import router as reports_router

router = APIRouter()
router.include_router(wizard_router)
router.include_router(reports_router)
