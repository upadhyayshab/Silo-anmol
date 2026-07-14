from fastapi import APIRouter, BackgroundTasks, Body

from config import get_settings, get_engine
from services import process_store_order, process_store_lead

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter(prefix="/store", tags=["Store (Medusa)"])


@router.post("/order")
async def store_order(background_tasks: BackgroundTasks,
                      payload: dict = Body(...)):
    """Medusa order.placed -> create/upsert lead + order in the internal CRM."""
    background_tasks.add_task(process_store_order, engine, payload)
    return {"received": True}


@router.post("/lead")
async def store_lead(background_tasks: BackgroundTasks,
                     payload: dict = Body(...)):
    """Medusa customer.* -> create/upsert a bare lead in the internal CRM."""
    background_tasks.add_task(process_store_lead, engine, payload)
    return {"received": True}
