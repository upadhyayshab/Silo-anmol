"""Daily job: revert stale (non-terminal, non-pending) orders back to PENDING.

Super-admin gated via the app_settings row auto_revert_enabled. An order reverts once
its status has been untouched for 24h; the clock is the latest delivery_tracking row
(fallback the order's updated_at, then created_at). While flush is off, the pre-existing
backlog is anchored at the auto_revert_baseline_at setting so it only reverts 24h after
go-live. Each revert writes a source='system' tracking row and a lead-timeline activity.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import get_engine, get_settings
from managers import (
    CustomerOrderManager, DeliveryTrackingManager, DeliveryTrackingSchema,
    AppSettingManager,
)
from utils.constants import (
    OrderStatus, SYSTEM_USER_UID, is_revertible_status,
    SETTING_AUTO_REVERT_ENABLED, SETTING_AUTO_REVERT_FLUSH, SETTING_AUTO_REVERT_BASELINE_AT,
)

logger = logging.getLogger(__name__)

REVERT_AFTER = timedelta(hours=24)


def should_revert(clock: Optional[datetime], now: datetime, *,
                  baseline: Optional[datetime], flush: bool) -> bool:
    """Pure eligibility test. `clock` = when the status last changed (already
    fallback-resolved by the caller). True once it has sat >= 24h — anchored at
    `baseline` for the pre-existing backlog unless `flush` is on."""
    if clock is None:
        return False
    effective = clock
    if not flush and baseline is not None and effective < baseline:
        effective = baseline
    return effective <= now - REVERT_AFTER


async def revert_stale_orders():
    """Scheduled at midnight IST. No-op unless a super admin has enabled it."""
    engine = get_engine(get_settings().name)
    kv = await AppSettingManager(engine).get_map(
        [SETTING_AUTO_REVERT_ENABLED, SETTING_AUTO_REVERT_FLUSH, SETTING_AUTO_REVERT_BASELINE_AT])
    if not kv.get(SETTING_AUTO_REVERT_ENABLED):
        return {"reverted": 0, "reason": "disabled"}

    order_manager = CustomerOrderManager(engine)
    tracking_manager = DeliveryTrackingManager(engine)

    revertible = [s for s in OrderStatus if is_revertible_status(s)]
    # ponytail: single scan of the in-scope orders (~hundreds); high limit, no paging.
    orders = await order_manager.fetch_all(filters={"order_status": revertible}, limit=100000)
    order_ids = [o.uid for o in orders.items]
    last_change = await tracking_manager.last_change_at(order_ids)

    now = datetime.now(timezone.utc)
    baseline_raw = kv.get(SETTING_AUTO_REVERT_BASELINE_AT)
    baseline = datetime.fromisoformat(baseline_raw) if baseline_raw else None
    flush = bool(kv.get(SETTING_AUTO_REVERT_FLUSH, False))

    reverted = 0
    for order in orders.items:
        clock = last_change.get(order.uid) or order.updated_at or order.created_at
        if should_revert(clock, now, baseline=baseline, flush=flush):
            await _revert_one(engine, order_manager, tracking_manager, order)
            reverted += 1
    logger.info("revert_stale_orders: reverted %s/%s stale orders", reverted, len(order_ids))
    return {"reverted": reverted, "scanned": len(order_ids)}


async def _revert_one(engine, order_manager, tracking_manager, order):
    old = order.order_status
    old_val = old.value if hasattr(old, "value") else str(old)
    remark = f"Auto-reverted to pending after 24h (was {old_val})"

    await order_manager.update(order.uid, {
        "order_status": OrderStatus.PENDING,
        "delivery_person_id": None,
        "actual_delivery_date": None,
        "status_remarks": remark,
    })

    # Audit row, attributed to the seeded system user. Best-effort (NOT NULL guard).
    if order.assigned_outlet_id and order.telecaller_id:
        try:
            await tracking_manager.create(DeliveryTrackingSchema(
                order_id=order.uid,
                outlet_id=order.assigned_outlet_id,
                telecaller_id=order.telecaller_id,
                delivery_person_id=None,
                status_changed_to=OrderStatus.PENDING,
                remarks=remark,
                changed_by=SYSTEM_USER_UID,
                source="system",
            ))
        except Exception as e:
            logger.warning("revert tracking row not written for %s: %s: %s",
                           order.uid, type(e).__name__, e)

    # The "activity on revert" — lands on the linked lead's timeline (no-op if no lead).
    try:
        from services import leadService
        await leadService.log_order_status_change(
            engine, order, OrderStatus.PENDING, SYSTEM_USER_UID,
            old_status=old, remarks="Auto-reverted after 24h")
    except Exception as e:
        logger.warning("revert activity not logged for %s: %s", order.uid, e)
