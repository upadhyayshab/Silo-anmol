from fastapi import APIRouter

from config import get_settings, get_engine
from models import RecommendationRequest, RecommendationResponse
from utils.scoringEngine import CropScoringEngine

router = APIRouter(prefix="/recommendations")
settings = get_settings()
engine = get_engine(settings.name)
scoring_engine = CropScoringEngine(engine)


@router.post("", response_model=RecommendationResponse)
async def post_recommendations(payload: RecommendationRequest):
    return await scoring_engine.recommend(payload)
