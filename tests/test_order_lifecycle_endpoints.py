"""Webhook + lifecycle endpoints via fake-swap. Run: python tests/test_order_lifecycle_endpoints.py"""
import os, sys, asyncio
from types import SimpleNamespace
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa


class FakeTracking:
    """Mimics DeliveryTrackingManager for one order: fetch_all(sorts) + create."""
    def __init__(self, rows=None):
        self.rows = rows or []
    async def fetch_all(self, limit=0, offset=0, filters=None, sorts=None, **kw):
        return SimpleNamespace(items=list(self.rows), count=len(self.rows))
    async def create(self, row, session=None, **kw):
        row.created_at = datetime.now(timezone.utc)
        self.rows.append(row)
        return row


class FakeOrderManager:
    def __init__(self, order):
        self.order = order
        self.updates = []
    async def fetch(self, uid, **kw):
        return self.order
    async def fetch_all(self, limit=0, offset=0, filters=None, sorts=None, **kw):
        return SimpleNamespace(items=[self.order], count=1)
    async def update(self, uid, updates, **kw):
        self.updates.append(updates)
        for k, v in updates.items():
            setattr(self.order, k, v)
        return self.order


def _order(**kw):
    base = dict(uid="o1", order_number="ORD-1", order_status="attempted",
                assigned_outlet_id="out1", telecaller_id="t1", delivery_person_id="d1",
                priority_level=0, total_amount=0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_third_disposition_writes_escalation_event():
    import routers.v1.delivery_guys as W
    import services.order_events_service as SVC
    from fastapi import BackgroundTasks
    # The webhook payload model is defined locally in delivery_guys.py, not app/models.
    from routers.v1.delivery_guys import DeliveryStatusUpdatePayload

    order = _order()
    # two prior RIDER_DISPOSITION events already logged
    prior = [SimpleNamespace(event_type="RIDER_DISPOSITION", status_changed_to="attempted",
                             created_at=datetime.now(timezone.utc), payload=None) for _ in range(2)]
    ftrack = FakeTracking(rows=list(prior))
    fom = FakeOrderManager(order)

    orig = (W.order_manager, W.tracking_manager, SVC.tracking_manager)
    W.order_manager = fom
    W.tracking_manager = ftrack
    SVC.tracking_manager = ftrack
    try:
        bt = BackgroundTasks()
        payload = [DeliveryStatusUpdatePayload(order_id="o1", status="customer_not_available",
                                               delivery_person_id="d1")]
        # Auth is a FastAPI Depends() default — not invoked when calling the coroutine
        # directly, so no ctx arg is needed here.
        asyncio.run(W.update_delivery_status(payload, bt))
    finally:
        W.order_manager, W.tracking_manager, SVC.tracking_manager = orig

    types = [r.event_type for r in ftrack.rows]
    assert types.count("RIDER_DISPOSITION") == 3   # 2 prior + this one
    assert "ESCALATED_CRM" in types                # 3rd attempt escalated
    print("OK: test_third_disposition_writes_escalation_event")


if __name__ == "__main__":
    test_third_disposition_writes_escalation_event()
    print("All tests passed.")
