"""Order lifecycle event log: append-only writer + pure derivation (fold) + escalation rule.

The log (delivery_tracking rows tagged with event_type) is the source of truth; all workflow
state is derived by fold_order_state — no denormalized columns. See the design doc §3.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional

from config import get_engine, get_settings
from managers import DeliveryTrackingManager, DeliveryTrackingSchema
from utils.constants import (
    OrderEventType, EscalationState, Custody, NON_DELIVERED_RIDER_OUTCOMES,
)

settings = get_settings()
engine = get_engine(settings.name)
tracking_manager = DeliveryTrackingManager(engine)

ATTEMPTS_PER_CYCLE = 3


@dataclass
class OrderState:
    custody: str = Custody.OUTLET
    attempt_count: int = 0
    escalation_count: int = 0
    escalated_at: Optional[datetime] = None
    escalation_state: str = EscalationState.NONE
    cancellation_reason: Optional[str] = None


def fold_order_state(events: List[Any]) -> OrderState:
    """Fold one order's events (ascending by created_at) into derived state. Pure."""
    s = OrderState()
    for e in events:
        et = e.event_type
        if et == OrderEventType.ASSIGNED:
            s.custody = Custody.RIDER
        elif et == OrderEventType.RIDER_DISPOSITION:
            outcome = (e.status_changed_to or "").lower()
            if outcome in NON_DELIVERED_RIDER_OUTCOMES:
                s.attempt_count += 1
                s.custody = Custody.RIDER
            elif outcome == "delivered":
                s.custody = Custody.CUSTOMER
        elif et == OrderEventType.RETURNED_TO_OUTLET:
            s.custody = Custody.OUTLET
        elif et == OrderEventType.ESCALATED_CRM:
            s.escalation_state = EscalationState.CRM_REVIEW
            if s.escalated_at is None:            # first escalation only; never resets
                s.escalated_at = e.created_at
        elif et == OrderEventType.ESCALATED_LOGISTICS:
            s.escalation_state = EscalationState.LOGISTICS
            s.escalation_count += 1
            s.custody = Custody.OUTLET
        elif et == OrderEventType.CANCELLED:
            s.escalation_state = EscalationState.NONE
            if e.payload:
                s.cancellation_reason = e.payload.get("cancellation_reason")
    return s


def should_escalate(state: OrderState) -> bool:
    """Fire an ESCALATED_CRM when the fresh cycle of 3 is complete and not already in CRM."""
    if state.escalation_state == EscalationState.CRM_REVIEW:
        return False
    return state.attempt_count >= ATTEMPTS_PER_CYCLE * (state.escalation_count + 1)


async def load_events(order_id: str) -> List[Any]:
    """All events for an order, ascending by created_at (leading '-' = ASC in this codebase)."""
    res = await tracking_manager.fetch_all(filters={"order_id": order_id}, sorts=["-created_at"])
    return res.items


async def order_ids_with_event(event_type, outlet_id: Optional[str] = None) -> List[str]:
    """Distinct order_ids that have >=1 event of this type (optionally scoped to an outlet).
    Lets a list endpoint fold only the relevant handful of orders instead of every
    non-terminal order at the outlet. Uses scalar filters only (no dict operators)."""
    f = {"event_type": event_type}
    if outlet_id:
        f["outlet_id"] = outlet_id
    res = await tracking_manager.fetch_all(filters=f)
    return list({e.order_id for e in res.items})


async def load_events_bulk(order_ids: List[str]) -> dict:
    """Events for many orders in ONE query, grouped {order_id: [events ascending]}.
    List endpoints (returns / crm-queue) fold every candidate order; calling load_events
    per order is an N+1 that is catastrophic against a remote DB (one round-trip per order).
    A single IN query + in-memory grouping turns N+1 into 1. Per-order lists stay ascending
    because the whole result is sorted ascending by created_at."""
    ids = [i for i in order_ids if i]
    if not ids:
        return {}
    res = await tracking_manager.fetch_all(filters={"order_id": ids}, sorts=["-created_at"])
    grouped: dict = {}
    for e in res.items:
        grouped.setdefault(e.order_id, []).append(e)
    return grouped


async def attempt_counts_for(order_ids: List[str]) -> dict:
    """{order_id: attempt_count} for many orders via ONE batched delivery_tracking query
    (load_events_bulk groups by order_id in Python) + fold_order_state per group — no
    per-order fold query. Orders with no delivery_tracking events are simply absent from
    load_events_bulk's result, so callers should default with .get(order_id, 0).
    Shared by any order-list endpoint that needs to surface attempt_count (SA export +
    outlet order view both read GET /orders, so one call here covers both).
    # ponytail: fold on read; persist attempt_count column if this export's latency grows
    """
    events_by_order = await load_events_bulk(order_ids)
    return {oid: fold_order_state(events).attempt_count for oid, events in events_by_order.items()}


async def record_event(order, event_type, *, actor_id, source, status=None,
                       remarks=None, postpone_date=None, payload=None, session=None):
    """Append one event row. outlet_id may be None for orders not yet assigned to an
    outlet (the column is nullable); telecaller_id is always set on a real order."""
    row = DeliveryTrackingSchema(
        order_id=order.uid,
        outlet_id=order.assigned_outlet_id,
        telecaller_id=order.telecaller_id,
        delivery_person_id=getattr(order, "delivery_person_id", None),
        status_changed_to=(status if status is not None else (order.order_status or "")),
        event_type=event_type,
        source=source,
        postpone_date=postpone_date,
        priority_level=order.priority_level or 0,
        remarks=remarks,
        payload=payload,
        changed_by=actor_id,
    )
    return await tracking_manager.create(row, session=session)
