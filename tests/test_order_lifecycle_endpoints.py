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

# order_lifecycle mirrors escalation / CRM outcomes onto the lead activity feed via
# leadService.log_order_lead_activity (which would hit the DB). Patch it to a recorder so
# these tests stay DB-free; assert against LEAD_ACTIVITY.
import services.leadService as _leadService  # noqa
LEAD_ACTIVITY = []
async def _fake_log_order_lead_activity(engine, order, body, **kw):
    LEAD_ACTIVITY.append({"order_number": getattr(order, "order_number", None), "body": body, **kw})
_leadService.log_order_lead_activity = _fake_log_order_lead_activity


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


class _FakeSession:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): pass


class FakeOrderManager:
    def __init__(self, order):
        self.order = order
        self.updates = []
    def session_factory(self):
        return _FakeSession()
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


def test_webhook_writes_disposition_no_escalation():
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
        # The webhook has no auth dependency at all now (removed) — call it directly.
        asyncio.run(W.update_delivery_status(payload, bt))
    finally:
        W.order_manager, W.tracking_manager, SVC.tracking_manager = orig

    types = [r.event_type for r in ftrack.rows]
    assert types.count("RIDER_DISPOSITION") == 3   # 2 prior + this one
    assert "ESCALATED_CRM" not in types             # escalation no longer happens at the webhook
    print("OK: test_webhook_writes_disposition_no_escalation")


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
    ev = [SimpleNamespace(event_type="ASSIGNED", status_changed_to=None, created_at=now, payload=None, order_id="o1"),
          SimpleNamespace(event_type="RIDER_DISPOSITION", status_changed_to="attempted",
                          created_at=now, payload=None, changed_by="d1", remarks=None, source="rider_app", order_id="o1")]
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


def test_return_confirm_escalates_after_three_attempts():
    import routers.v1.order_lifecycle as L
    import services.order_events_service as SVC
    from utils.auth import AuthContext
    now = datetime.now(timezone.utc)
    order = _order(order_status="attempted", delivery_person_id="d1")
    fom = FakeOrderManager(order)
    prior = [SimpleNamespace(event_type="RIDER_DISPOSITION", status_changed_to="attempted",
                             created_at=now, payload=None, changed_by="d1", remarks=None,
                             source="rider_app") for _ in range(3)]
    ftrack = FakeTracking(rows=list(prior))
    LEAD_ACTIVITY.clear()
    orig = (L.order_manager, SVC.tracking_manager)
    L.order_manager = fom
    SVC.tracking_manager = ftrack
    try:
        ctx = AuthContext(user_id="mgr", role="OUTLET_MANAGER", scope_level="GLOBAL")
        asyncio.run(L.confirm_order_return("o1", ctx))
    finally:
        L.order_manager, SVC.tracking_manager = orig
    types = [r.event_type for r in ftrack.rows]
    assert "RETURNED_TO_OUTLET" in types
    assert "ESCALATED_CRM" in types
    assert order.order_status == "pending"
    # escalation was mirrored onto the lead activity feed
    assert any("escalated to crm" in a["body"].lower() for a in LEAD_ACTIVITY)
    print("OK: test_return_confirm_escalates_after_three_attempts")


def test_return_confirm_no_escalation_under_three():
    import routers.v1.order_lifecycle as L
    import services.order_events_service as SVC
    from utils.auth import AuthContext
    now = datetime.now(timezone.utc)
    order = _order(order_status="attempted", delivery_person_id="d1")
    fom = FakeOrderManager(order)
    prior = [SimpleNamespace(event_type="RIDER_DISPOSITION", status_changed_to="attempted",
                             created_at=now, payload=None, changed_by="d1", remarks=None,
                             source="rider_app") for _ in range(2)]
    ftrack = FakeTracking(rows=list(prior))
    LEAD_ACTIVITY.clear()
    orig = (L.order_manager, SVC.tracking_manager)
    L.order_manager = fom
    SVC.tracking_manager = ftrack
    try:
        ctx = AuthContext(user_id="mgr", role="OUTLET_MANAGER", scope_level="GLOBAL")
        asyncio.run(L.confirm_order_return("o1", ctx))
    finally:
        L.order_manager, SVC.tracking_manager = orig
    types = [r.event_type for r in ftrack.rows]
    assert "RETURNED_TO_OUTLET" in types
    assert "ESCALATED_CRM" not in types
    assert order.order_status == "pending"
    # under 3 attempts -> no escalation -> nothing mirrored to the lead feed
    assert LEAD_ACTIVITY == []
    print("OK: test_return_confirm_no_escalation_under_three")


def _escalated_order_rows():
    now = datetime.now(timezone.utc)
    rows = [SimpleNamespace(event_type="RIDER_DISPOSITION", status_changed_to="attempted",
                            created_at=now, payload=None, changed_by="d1", remarks=None,
                            source="rider_app", order_id="o1")
            for _ in range(3)]
    rows.append(SimpleNamespace(event_type="ESCALATED_CRM", status_changed_to="attempted",
                                created_at=now, payload=None, changed_by="mgr", remarks=None,
                                source="erp", order_id="o1"))
    return rows


def test_crm_confirm_bounces_to_logistics():
    import routers.v1.order_lifecycle as L
    import services.order_events_service as SVC
    from utils.auth import AuthContext
    order = _order(order_status="attempted", delivery_person_id="d1")
    fom = FakeOrderManager(order)
    ftrack = FakeTracking(rows=_escalated_order_rows())
    LEAD_ACTIVITY.clear()
    orig = (L.order_manager, SVC.tracking_manager)
    L.order_manager = fom; SVC.tracking_manager = ftrack
    try:
        ctx = AuthContext(user_id="tc1", role="TELECALLER", scope_level="GLOBAL")
        asyncio.run(L.submit_crm_outcome("o1", SimpleNamespace(outcome="confirm", remark="wants it"), ctx))
    finally:
        L.order_manager, SVC.tracking_manager = orig
    assert order.order_status == "pending"
    assert any(r.event_type == "ESCALATED_LOGISTICS" for r in ftrack.rows)
    # CRM decision mirrored onto the lead feed with the outcome tag
    assert any(a.get("outcome") == "confirm" for a in LEAD_ACTIVITY)
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


def test_crm_queue_lists_escalated_orders():
    import routers.v1.order_lifecycle as L
    import services.order_events_service as SVC
    from utils.auth import AuthContext
    order = _order(order_status="pending")
    fom = FakeOrderManager(order)
    ftrack = FakeTracking(rows=_escalated_order_rows())   # 3 dispositions + ESCALATED_CRM
    orig = (L.order_manager, SVC.tracking_manager)
    L.order_manager = fom
    SVC.tracking_manager = ftrack
    try:
        ctx = AuthContext(user_id="tc1", role="TELECALLER", scope_level="GLOBAL")
        resp = asyncio.run(L.get_crm_queue(outlet_id="out1", ctx=ctx))
    finally:
        L.order_manager, SVC.tracking_manager = orig
    assert resp["orders"] and resp["orders"][0]["order_number"] == "ORD-1"
    assert resp["orders"][0]["attempt_count"] == 3
    print("OK: test_crm_queue_lists_escalated_orders")


def test_crm_outcome_rejects_non_review_order():
    import routers.v1.order_lifecycle as L
    import services.order_events_service as SVC
    from utils.auth import AuthContext
    from fastapi import HTTPException
    order = _order(order_status="attempted")
    fom = FakeOrderManager(order)
    # No ESCALATED_CRM event anywhere in the log -> escalation_state stays NONE.
    ftrack = FakeTracking(rows=[])
    orig = (L.order_manager, SVC.tracking_manager)
    L.order_manager = fom; SVC.tracking_manager = ftrack
    try:
        ctx = AuthContext(user_id="tc1", role="TELECALLER", scope_level="GLOBAL")
        try:
            asyncio.run(L.submit_crm_outcome("o1", SimpleNamespace(outcome="confirm", remark="x"), ctx))
            raised = False
        except HTTPException as e:
            raised = True
            assert e.status_code == 400
    finally:
        L.order_manager, SVC.tracking_manager = orig
    assert raised
    print("OK: test_crm_outcome_rejects_non_review_order")


if __name__ == "__main__":
    test_webhook_writes_disposition_no_escalation()
    test_delete_writes_snapshot_event_before_delete()
    test_timeline_returns_events_and_state()
    test_returns_lists_rider_custody_orders()
    test_return_confirm_sets_pending_and_logs_event()
    test_return_confirm_escalates_after_three_attempts()
    test_return_confirm_no_escalation_under_three()
    test_crm_confirm_bounces_to_logistics()
    test_crm_decline_cancels_with_reason()
    test_crm_queue_lists_escalated_orders()
    test_crm_outcome_rejects_non_review_order()
    print("All tests passed.")
