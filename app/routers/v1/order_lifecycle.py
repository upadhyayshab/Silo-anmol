"""Order lifecycle endpoints: timeline, rider returns, CRM escalation queue.
Kept separate from the (very large) orders.py."""
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from config import get_settings, get_engine
from managers import CustomerOrderManager, UserManager
from models import CrmOutcomeRequest
from services import order_events_service
from services.order_events_service import fold_order_state, should_escalate
from services.order_aging_service import AGE_LIMIT
from utils.auth import AuthContext, require_permission
from utils.constants import (
    OrderStatus, OrderEventType, Custody, TERMINAL_ORDER_STATUSES,
    CancellationReason, EscalationState,
)
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


@router.get("/returns")
async def get_rider_returns(
    outlet_id: Optional[str] = None,
    delivery_person_id: Optional[str] = None,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_STATUS)),
):
    """Orders physically still with a rider (custody=RIDER), grouped by rider.
    // ponytail: scans non-terminal orders and folds each — same pattern as the old
    revert job. Materialize a projection only if this scan ever gets slow."""
    filters = {"order_status": [s for s in OrderStatus if s not in TERMINAL_ORDER_STATUSES]}
    if outlet_id:
        filters["assigned_outlet_id"] = outlet_id
    if delivery_person_id:
        filters["delivery_person_id"] = delivery_person_id
    orders = (await order_manager.fetch_all(filters=filters, limit=100000)).items

    now = datetime.now(timezone.utc)
    groups = {}
    for o in orders:
        events = await order_events_service.load_events(o.uid)
        state = fold_order_state(events)
        if state.custody != Custody.RIDER:
            continue
        last_disp = max((e.created_at for e in events
                         if e.event_type == OrderEventType.RIDER_DISPOSITION), default=None)
        hours = round((now - last_disp).total_seconds() / 3600, 1) if last_disp else None
        days_left = None
        if state.escalated_at:
            days_left = max(0, (AGE_LIMIT - (now - state.escalated_at)).days)
        g = groups.setdefault(o.delivery_person_id, {
            "delivery_person_id": o.delivery_person_id, "orders": []})
        g["orders"].append({
            "uid": o.uid, "order_number": o.order_number,
            "customer_name": getattr(o, "customer_name", None), "attempt_count": state.attempt_count,
            "with_rider_hours": hours,
            "escalated_at": state.escalated_at, "days_left": days_left,
        })
    names = await _actor_names(list(groups.keys()))
    for pid, g in groups.items():
        g["delivery_person_name"] = names.get(pid)
    return {"riders": list(groups.values())}


@router.post("/{order_id}/return")
async def confirm_order_return(
    order_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_STATUS)),
):
    """Outlet manager confirms undelivered goods are physically back at the outlet."""
    order = await order_manager.fetch(order_id)
    if order.order_status in TERMINAL_ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="Order is already delivered or cancelled.")
    await order_manager.update(order_id, {
        "order_status": OrderStatus.PENDING, "delivery_person_id": None})
    order.order_status = OrderStatus.PENDING
    order.delivery_person_id = None
    await order_events_service.record_event(
        order, OrderEventType.RETURNED_TO_OUTLET,
        actor_id=ctx.user_id, source="erp", status=OrderStatus.PENDING,
        remarks="Undelivered goods received back at outlet.")

    # Fold the full log and escalate if the fresh cycle of 3 is complete. Goods are
    # physically back at the outlet before CRM ever sees the order (design §4).
    events = await order_events_service.load_events(order_id)
    state = fold_order_state(events)
    if should_escalate(state):
        await order_events_service.record_event(
            order, OrderEventType.ESCALATED_CRM,
            actor_id=ctx.user_id, source="erp",
            status=order.order_status,
            remarks=f"Auto-escalated to CRM on return after {state.attempt_count} attempts.",
            payload={"attempt_count": state.attempt_count,
                     "escalation_count": state.escalation_count},
        )
    return {"status": "ok"}


@router.get("/crm-queue")
async def get_crm_queue(
    outlet_id: Optional[str] = None,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_READ)),
):
    """Orders escalated to CRM and awaiting a confirm/decline/unreachable outcome."""
    filters = {"order_status": [s for s in OrderStatus if s not in TERMINAL_ORDER_STATUSES]}
    if outlet_id:
        filters["assigned_outlet_id"] = outlet_id
    orders = (await order_manager.fetch_all(filters=filters, limit=100000)).items
    now = datetime.now(timezone.utc)
    out = []
    for o in orders:
        state = fold_order_state(await order_events_service.load_events(o.uid))
        if state.escalation_state != EscalationState.CRM_REVIEW:
            continue
        days_left = None
        if state.escalated_at:
            days_left = max(0, (AGE_LIMIT - (now - state.escalated_at)).days)
        out.append({
            "uid": o.uid, "order_number": o.order_number, "customer_name": getattr(o, "customer_name", None),
            "customer_phone": getattr(o, "customer_phone", None), "attempt_count": state.attempt_count,
            "escalation_count": state.escalation_count,
            "escalated_at": state.escalated_at, "days_left": days_left,
        })
    return {"orders": out}


@router.post("/{order_id}/crm-outcome")
async def submit_crm_outcome(
    order_id: str,
    payload: CrmOutcomeRequest,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_WRITE)),
):
    """CRM disposition for an order sitting in escalation_state=CRM_REVIEW."""
    order = await order_manager.fetch(order_id)
    state = fold_order_state(await order_events_service.load_events(order_id))
    if state.escalation_state != EscalationState.CRM_REVIEW:
        raise HTTPException(status_code=400, detail="Order is not under CRM review.")
    outcome = payload.outcome
    if outcome == "confirm":
        await order_manager.update(order_id, {
            "order_status": OrderStatus.PENDING, "delivery_person_id": None})
        order.order_status = OrderStatus.PENDING
        order.delivery_person_id = None
        await order_events_service.record_event(
            order, OrderEventType.ESCALATED_LOGISTICS, actor_id=ctx.user_id, source="erp",
            status=OrderStatus.PENDING,
            remarks=payload.remark or "Customer confirmed — back to logistics.")
    elif outcome == "decline":
        await order_manager.update(order_id, {"order_status": OrderStatus.CANCELLED,
                                              "status_remarks": payload.remark})
        order.order_status = OrderStatus.CANCELLED
        await order_events_service.record_event(
            order, OrderEventType.CANCELLED, actor_id=ctx.user_id, source="erp",
            status=OrderStatus.CANCELLED, remarks=payload.remark,
            payload={"cancellation_reason": CancellationReason.CUSTOMER_DECLINED,
                     "cancelled_by_role": ctx.role})
    elif outcome == "unreachable":
        await order_events_service.record_event(
            order, OrderEventType.CRM_OUTCOME, actor_id=ctx.user_id, source="erp",
            status=order.order_status, remarks=payload.remark or "Customer unreachable.",
            payload={"crm_outcome": "unreachable"})
    else:
        raise HTTPException(status_code=400, detail=f"Unknown outcome: {outcome}")
    return {"status": "ok", "outcome": outcome}
