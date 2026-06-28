from fastapi import APIRouter, BackgroundTasks, Body, Header, HTTPException, status

from config import get_settings, get_engine
from services import process_store_order, process_store_lead

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter(prefix="/store", tags=["Store (Medusa)"])


def _check_secret(secret: str | None) -> None:
    expected = settings.store_webhook_secret
    if not expected or secret != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid store webhook secret")


@router.post("/order")
async def store_order(background_tasks: BackgroundTasks,
                      payload: dict = Body(...),
                      x_store_webhook_secret: str | None = Header(None)):
    """Medusa order.placed -> create/upsert lead + order in the internal CRM."""
    _check_secret(x_store_webhook_secret)
    background_tasks.add_task(process_store_order, engine, payload)
    return {"received": True}


@router.post("/lead")
async def store_lead(background_tasks: BackgroundTasks,
                     payload: dict = Body(...),
                     x_store_webhook_secret: str | None = Header(None)):
    """Medusa customer.* -> create/upsert a bare lead in the internal CRM."""
    _check_secret(x_store_webhook_secret)
    background_tasks.add_task(process_store_lead, engine, payload)
    return {"received": True}
