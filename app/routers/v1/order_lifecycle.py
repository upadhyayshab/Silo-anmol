"""Order lifecycle endpoints: timeline, rider returns, CRM escalation queue.
Kept separate from the (very large) orders.py."""
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from config import get_settings, get_engine
from managers import CustomerOrderManager, UserManager
from services import order_events_service
from services.order_events_service import fold_order_state
from utils.auth import AuthContext, require_permission
from utils.permissions import Permission

settings = get_settings()
engine = get_engine(settings.name)
order_manager = CustomerOrderManager(engine)
user_manager = UserManager(engine)

router = APIRouter(prefix="/orders", tags=["Order Lifecycle"])


async def _actor_names(actor_ids):
    ids = [a for a in {*actor_ids} if a]
    if not ids:
        return {}
    res = await user_manager.fetch_all(filters={"uid": ids})
    return {u.uid: u.full_name for u in res.items}


@router.get("/{order_id}/timeline")
async def get_order_timeline(
    order_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_READ)),
):
    events = await order_events_service.load_events(order_id)
    if not events:
        raise HTTPException(status_code=404, detail="No lifecycle events for this order.")
    names = await _actor_names([getattr(e, "changed_by", None) for e in events])
    state = fold_order_state(events)
    return {
        "order_id": order_id,
        "state": asdict(state),
        "events": [{
            "event_type": e.event_type,
            "status": e.status_changed_to,
            "source": e.source,
            "actor_id": getattr(e, "changed_by", None),
            "actor_name": names.get(getattr(e, "changed_by", None)),
            "remarks": e.remarks,
            "payload": e.payload,
            "created_at": e.created_at,
        } for e in events],
    }
