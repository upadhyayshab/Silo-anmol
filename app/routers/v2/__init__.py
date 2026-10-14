from fastapi import APIRouter

from .wizard import router as wizard_router

router = APIRouter()
router.include_router(wizard_router)
