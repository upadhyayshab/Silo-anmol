from fastapi import APIRouter

from config import get_settings, get_engine
from managers import CropManager
from models import CropModel, ListResponse

router = APIRouter(prefix="/crops")
settings = get_settings()
engine = get_engine(settings.name)
crop_manager = CropManager(engine)


@router.get("", response_model=ListResponse[CropModel])
async def list_crops():
    """
    Full crop reference directory: every crop with its name in all 14 supported
    languages plus core attributes (code, family, horizon, water_need, cost_class).
    Public -- no auth required, same trust level as /options.
    """
    # A to Z: SharedBackend reads a bare or "+" sort as descending, "-" as ascending.
    crops = (await crop_manager.fetch_all(limit=0, sorts=["-name_en"])).items
    items = [CropModel(**c.model_dump()) for c in crops]
    return ListResponse[CropModel](items=items, count=len(items))
