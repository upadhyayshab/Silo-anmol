from fastapi import APIRouter, Depends

from config import get_settings, get_engine
from models import RecommendationRequest, RecommendationResponse
from utils.dependencies import master_key_dependency
from utils.scoringEngine import CropScoringEngine

# Raw scoring-engine access for internal tools only: it has no login and no
# subscription metering, so without the admin key it was free crop advice.
# Farmers use /api/v1/recommend and /api/v2/wizard/submit.
router = APIRouter(prefix="/recommendations", dependencies=[Depends(master_key_dependency)])
settings = get_settings()
engine = get_engine(settings.name)
scoring_engine = CropScoringEngine(engine)


@router.post("", response_model=RecommendationResponse)
async def post_recommendations(payload: RecommendationRequest):
    return await scoring_engine.recommend(payload)
