"""Nightly 30-day auto-cancel for escalated, unresolved orders. Kill-switch flagged."""
from datetime import datetime, timedelta, timezone
from typing import Optional

AGE_LIMIT = timedelta(days=30)


def is_aged_out(escalated_at: Optional[datetime], *, now: Optional[datetime] = None) -> bool:
    """True when an order has been in escalation >= 30 days. None escalated_at never ages."""
    if escalated_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - escalated_at) >= AGE_LIMIT


from config import get_settings, get_engine
from managers import CustomerOrderManager, AppSettingManager
from services import order_events_service
from services.order_events_service import fold_order_state
from utils.constants import (
    OrderStatus, OrderEventType, EscalationState, CancellationReason,
    TERMINAL_ORDER_STATUSES, SYSTEM_USER_UID, SETTING_AGING_CANCEL_ENABLED,
)

_settings = get_settings()
_engine = get_engine(_settings.name)
order_manager = CustomerOrderManager(_engine)
settings_manager = AppSettingManager(_engine)


async def cancel_aged_orders():
    """Nightly: cancel escalated orders unresolved >= 30 days. Kill-switch flagged (default off)."""
    flags = await settings_manager.get_map([SETTING_AGING_CANCEL_ENABLED])
    if not flags.get(SETTING_AGING_CANCEL_ENABLED):
        return  # kill-switch off -> do nothing
    filters = {"order_status": [s for s in OrderStatus if s not in TERMINAL_ORDER_STATUSES]}
    orders = (await order_manager.fetch_all(filters=filters, limit=100000)).items
    for o in orders:
        state = fold_order_state(await order_events_service.load_events(o.uid))
        if state.escalation_state == EscalationState.NONE:
            continue
        if not is_aged_out(state.escalated_at):
            continue
        await order_manager.update(o.uid, {"order_status": OrderStatus.CANCELLED,
                                           "status_remarks": "Auto-cancelled: unresolved 30 days."})
        o.order_status = OrderStatus.CANCELLED
        await order_events_service.record_event(
            o, OrderEventType.CANCELLED, actor_id=SYSTEM_USER_UID, source="system",
            status=OrderStatus.CANCELLED, remarks="Auto-cancelled: unresolved 30 days.",
            payload={"cancellation_reason": CancellationReason.AGED_OUT})
