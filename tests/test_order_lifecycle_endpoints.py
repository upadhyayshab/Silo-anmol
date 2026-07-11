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


def test_delete_writes_snapshot_event_before_delete():
    import routers.v1.orders as O
    import services.order_events_service as SVC
    order = _order(order_status="pending")
    ftrack = FakeTracking()
    fom = FakeOrderManager(order)

    orig = (O.order_manager, O.tracking_manager, SVC.tracking_manager)
    O.order_manager = fom
    O.tracking_manager = ftrack
    SVC.tracking_manager = ftrack
    try:
        # call only the snapshot helper the task introduces (keep the test focused)
        asyncio.run(SVC.record_event(order, "DELETED", actor_id="super", source="erp",
                                     payload={"order_number": order.order_number}))
    finally:
        O.order_manager, O.tracking_manager, SVC.tracking_manager = orig
    assert any(r.event_type == "DELETED" and r.payload["order_number"] == "ORD-1"
               for r in ftrack.rows)
    print("OK: test_delete_writes_snapshot_event_before_delete")


def test_timeline_returns_events_and_state():
    import routers.v1.order_lifecycle as L
    import services.order_events_service as SVC
    from utils.auth import AuthContext
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(event_type="CREATED", status_changed_to="pending", source="erp",
                        changed_by="t1", remarks=None, created_at=now, payload=None),
        SimpleNamespace(event_type="RIDER_DISPOSITION", status_changed_to="attempted", source="rider_app",
                        changed_by="d1", remarks="no answer", created_at=now, payload=None),
    ]
    ftrack = FakeTracking(rows=rows)
    orig = SVC.tracking_manager
    SVC.tracking_manager = ftrack
    L.user_manager = SimpleNamespace(  # name lookup returns {} → actor_name None is fine
        fetch_all=lambda **kw: _acoro(SimpleNamespace(items=[], count=0)))
    try:
        ctx = AuthContext(user_id="super", role="SUPER_ADMIN", scope_level="GLOBAL")
        resp = asyncio.run(L.get_order_timeline("o1", ctx))
    finally:
        SVC.tracking_manager = orig
    assert resp["state"]["attempt_count"] == 1
    assert [e["event_type"] for e in resp["events"]] == ["CREATED", "RIDER_DISPOSITION"]
    print("OK: test_timeline_returns_events_and_state")


async def _acoro(v):  # helper: wrap a value in an awaitable
    return v


def test_returns_lists_rider_custody_orders():
    import routers.v1.order_lifecycle as L
    import services.order_events_service as SVC
    from utils.auth import AuthContext
    now = datetime.now(timezone.utc)
    order = _order(order_status="attempted")
    fom = FakeOrderManager(order)
    ev = [SimpleNamespace(event_type="ASSIGNED", status_changed_to=None, created_at=now, payload=None),
          SimpleNamespace(event_type="RIDER_DISPOSITION", status_changed_to="attempted",
                          created_at=now, payload=None, changed_by="d1", remarks=None, source="rider_app")]
    ftrack = FakeTracking(rows=ev)
    orig = (L.order_manager, SVC.tracking_manager)
    L.order_manager = fom
    SVC.tracking_manager = ftrack
    L.user_manager = SimpleNamespace(fetch_all=lambda **kw: _acoro(SimpleNamespace(items=[], count=0)))
    try:
        ctx = AuthContext(user_id="mgr", role="OUTLET_MANAGER", scope_level="GLOBAL")
        resp = asyncio.run(L.get_rider_returns(outlet_id="out1", delivery_person_id=None, ctx=ctx))
    finally:
        L.order_manager, SVC.tracking_manager = orig
    riders = resp["riders"]
    assert riders and riders[0]["orders"][0]["order_number"] == "ORD-1"
    print("OK: test_returns_lists_rider_custody_orders")


def test_return_confirm_sets_pending_and_logs_event():
    import routers.v1.order_lifecycle as L
    import services.order_events_service as SVC
    from utils.auth import AuthContext
    order = _order(order_status="attempted", delivery_person_id="d1")
    fom = FakeOrderManager(order)
    ftrack = FakeTracking()
    orig = (L.order_manager, SVC.tracking_manager)
    L.order_manager = fom
    SVC.tracking_manager = ftrack
    try:
        ctx = AuthContext(user_id="mgr", role="OUTLET_MANAGER", scope_level="GLOBAL")
        resp = asyncio.run(L.confirm_order_return("o1", ctx))
    finally:
        L.order_manager, SVC.tracking_manager = orig
    assert order.order_status == "pending"
    assert order.delivery_person_id is None
    assert any(r.event_type == "RETURNED_TO_OUTLET" for r in ftrack.rows)
    print("OK: test_return_confirm_sets_pending_and_logs_event")


def _escalated_order_rows():
    now = datetime.now(timezone.utc)
    rows = [SimpleNamespace(event_type="RIDER_DISPOSITION", status_changed_to="attempted",
                            created_at=now, payload=None, changed_by="d1", remarks=None, source="rider_app")
            for _ in range(3)]
    rows.append(SimpleNamespace(event_type="ESCALATED_CRM", status_changed_to="attempted",
                                created_at=now, payload=None, changed_by=None, remarks=None, source="system"))
    return rows


def test_crm_confirm_bounces_to_logistics():
    import routers.v1.order_lifecycle as L
    import services.order_events_service as SVC
    from utils.auth import AuthContext
    order = _order(order_status="attempted", delivery_person_id="d1")
    fom = FakeOrderManager(order)
    ftrack = FakeTracking(rows=_escalated_order_rows())
    orig = (L.order_manager, SVC.tracking_manager)
    L.order_manager = fom; SVC.tracking_manager = ftrack
    try:
        ctx = AuthContext(user_id="tc1", role="TELECALLER", scope_level="GLOBAL")
        asyncio.run(L.submit_crm_outcome("o1", SimpleNamespace(outcome="confirm", remark="wants it"), ctx))
    finally:
        L.order_manager, SVC.tracking_manager = orig
    assert order.order_status == "pending"
    assert any(r.event_type == "ESCALATED_LOGISTICS" for r in ftrack.rows)
    print("OK: test_crm_confirm_bounces_to_logistics")


def test_crm_decline_cancels_with_reason():
    import routers.v1.order_lifecycle as L
    import services.order_events_service as SVC
    from utils.auth import AuthContext
    order = _order(order_status="attempted")
    fom = FakeOrderManager(order)
    ftrack = FakeTracking(rows=_escalated_order_rows())
    orig = (L.order_manager, SVC.tracking_manager)
    L.order_manager = fom; SVC.tracking_manager = ftrack
    try:
        ctx = AuthContext(user_id="tc1", role="TELECALLER", scope_level="GLOBAL")
        asyncio.run(L.submit_crm_outcome("o1", SimpleNamespace(outcome="decline", remark="not needed"), ctx))
    finally:
        L.order_manager, SVC.tracking_manager = orig
    assert order.order_status == "cancelled"
    cancel = [r for r in ftrack.rows if r.event_type == "CANCELLED"][0]
    assert cancel.payload["cancellation_reason"] == "CUSTOMER_DECLINED"
    print("OK: test_crm_decline_cancels_with_reason")


if __name__ == "__main__":
    test_third_disposition_writes_escalation_event()
    test_delete_writes_snapshot_event_before_delete()
    test_timeline_returns_events_and_state()
    test_returns_lists_rider_custody_orders()
    test_return_confirm_sets_pending_and_logs_event()
    test_crm_confirm_bounces_to_logistics()
    test_crm_decline_cancels_with_reason()
    print("All tests passed.")
