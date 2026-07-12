"""Guarded bulk order status update (DB-free).

POST /orders/bulk/status-update lets the super admin (orders:revoke) set many
orders' status from an uploaded file. Server-side typed guard: confirm_text
must equal the target status name. Transition rules are the single-endpoint
ones plus one bulk-only edge — CANCELLED -> PENDING (un-cancel). DELIVERED is
immutable in bulk; bad rows are skipped with a reason, never aborting the batch.

Mirrors tests/test_orders_scope.py: import path_setup first, fake the module
globals (order_manager / store_service / tracking_manager) on routers.v1.orders
and restore in finally. Run::

    python tests/test_bulk_order_status.py    # or: pytest tests/test_bulk_order_status.py
"""
import os
import sys
import asyncio
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:  # path_setup prints a banner; keep it from blowing up a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports


def test_transition_allowed_uncancel_edge():
    import routers.v1.orders as O
    from utils.constants import OrderStatus

    # normal rules still apply
    assert O._transition_allowed(OrderStatus.PENDING, OrderStatus.CANCELLED)
    assert not O._transition_allowed(OrderStatus.CANCELLED, OrderStatus.PENDING)
    # the bulk-only edge: un-cancel back to pending
    assert O._transition_allowed(OrderStatus.CANCELLED, OrderStatus.PENDING, allow_uncancel=True)
    # the edge is ONLY cancelled->pending — nothing else widens
    assert not O._transition_allowed(OrderStatus.CANCELLED, OrderStatus.DELIVERY_ALLOTTED, allow_uncancel=True)
    # delivered stays final even with the flag
    assert not O._transition_allowed(OrderStatus.DELIVERED, OrderStatus.PENDING, allow_uncancel=True)
    assert not O._transition_allowed(OrderStatus.DELIVERED, OrderStatus.CANCELLED, allow_uncancel=True)


# --- bulk endpoint -----------------------------------------------------------


def _mk_order(number, order_status, lead_id=None):
    return SimpleNamespace(
        uid=f"uid-{number}",
        order_number=number,
        order_status=order_status,
        lead_id=lead_id,
        assigned_outlet_id="outlet-1",
        telecaller_id="tc-1",
        delivery_person_id=None,
        priority_level=0,
    )


class FakeOrderManager:
    def __init__(self, orders):
        self.orders = list(orders)
        self.updates = []

    async def fetch_all(self, limit=0, offset=0, filters=None, sorts=None, *, joins=None, **kw):
        nums = set(filters["order_number"])
        return SimpleNamespace(items=[o for o in self.orders if o.order_number in nums])

    async def update(self, uid, data):
        self.updates.append((uid, data))


class FakeStore:
    def __init__(self):
        self.cancelled = []

    async def order_cancelled(self, oid):
        self.cancelled.append(oid)

    async def order_delivered(self, oid):
        pass

    async def order_fulfilled(self, oid):
        pass


class FakeTracking:
    def __init__(self):
        self.created = []

    async def fetch_all(self, *a, **kw):
        return SimpleNamespace(items=[])

    async def create(self, rec):
        self.created.append(rec)


def _run_bulk(orders, **payload_kw):
    """Patch orders.py module globals, run the endpoint, restore in finally."""
    import routers.v1.orders as O
    import services.order_events_service as SVC
    from fastapi import BackgroundTasks
    from models import BulkOrderStatusUpdateRequest
    from utils.auth import AuthContext

    fom, fstore, ftrack = FakeOrderManager(orders), FakeStore(), FakeTracking()
    bt = BackgroundTasks()
    ctx = AuthContext(user_id="super", role="SUPER_ADMIN", scope_level="GLOBAL")
    orig = (O.order_manager, O.store_service, O.tracking_manager, SVC.tracking_manager)
    O.order_manager, O.store_service, O.tracking_manager = fom, fstore, ftrack
    SVC.tracking_manager = ftrack  # _apply_status_change's CRM-review guard folds events via SVC
    try:
        resp = asyncio.run(O.bulk_update_order_status(
            BulkOrderStatusUpdateRequest(**payload_kw), bt, ctx))
        return resp, fom, fstore, ftrack, bt
    finally:
        O.order_manager, O.store_service, O.tracking_manager, SVC.tracking_manager = orig


def _expect_400(fn):
    from fastapi import HTTPException
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == 400, f"expected 400, got {e.status_code}"
        return e
    raise AssertionError("expected HTTPException(400)")


def test_super_admin_holds_orders_revoke():
    from utils.permissions import has_permission, set_role_cache
    set_role_cache({})  # resolve against the code seed, not a loaded DB cache
    assert has_permission("SUPER_ADMIN", "orders:revoke")
    assert not has_permission("ADMIN", "orders:revoke")


def test_confirm_text_guard_rejects_and_touches_nothing():
    from utils.constants import OrderStatus
    orders = [_mk_order("ORD-1", OrderStatus.PENDING)]
    err = _expect_400(lambda: _run_bulk(
        orders, order_numbers=["ORD-1"], order_status="cancelled",
        status_remarks="bulk fix", confirm_text="delete"))
    assert "confirm" in str(err.detail).lower()


def test_remarks_required_for_cancelled():
    _expect_400(lambda: _run_bulk(
        [], order_numbers=["ORD-1"], order_status="cancelled",
        status_remarks=None, confirm_text="cancelled"))


def test_bulk_cancel_happy_path():
    from utils.constants import OrderStatus
    orders = [_mk_order("ORD-1", OrderStatus.PENDING),
              _mk_order("ORD-2", OrderStatus.DELIVERY_ALLOTTED)]
    resp, fom, fstore, _, _ = _run_bulk(
        orders, order_numbers=["ORD-1", "ORD-2"], order_status="cancelled",
        status_remarks="bulk cancellation", confirm_text="  Cancelled ")  # guard is trim+case-insensitive
    assert (resp.total, resp.updated, resp.skipped) == (2, 2, [])
    assert {u[0] for u in fom.updates} == {"uid-ORD-1", "uid-ORD-2"}
    assert all(u[1]["order_status"] == OrderStatus.CANCELLED for u in fom.updates)
    assert set(fstore.cancelled) == {"uid-ORD-1", "uid-ORD-2"}


def test_uncancel_to_pending():
    from utils.constants import OrderStatus
    resp, fom, _, ftrack, _ = _run_bulk(
        [_mk_order("ORD-1", OrderStatus.CANCELLED)],
        order_numbers=["ORD-1"], order_status="pending",
        status_remarks="reactivating wrongly cancelled order", confirm_text="pending")
    assert (resp.updated, resp.skipped) == (1, [])
    assert fom.updates[0][1]["order_status"] == OrderStatus.PENDING
    assert len(ftrack.created) == 1  # unassignment tracking record


def test_delivered_is_immutable():
    from utils.constants import OrderStatus
    resp, fom, _, _, _ = _run_bulk(
        [_mk_order("ORD-1", OrderStatus.DELIVERED)],
        order_numbers=["ORD-1"], order_status="cancelled",
        status_remarks="x", confirm_text="cancelled")
    assert resp.updated == 0 and fom.updates == []
    assert "delivered" in resp.skipped[0].reason.lower()


def test_invalid_transition_row_skipped_not_fatal():
    from utils.constants import OrderStatus
    orders = [_mk_order("ORD-1", OrderStatus.CANCELLED),  # cancelled -> delivery_allotted: not allowed
              _mk_order("ORD-2", OrderStatus.PENDING)]
    resp, fom, _, _, _ = _run_bulk(
        orders, order_numbers=["ORD-1", "ORD-2"], order_status="delivery_allotted",
        status_remarks=None, confirm_text="delivery_allotted")
    assert resp.updated == 1
    assert resp.skipped[0].order_number == "ORD-1"
    assert "transition" in resp.skipped[0].reason.lower()


def test_unknown_number_skipped_batch_continues():
    from utils.constants import OrderStatus
    resp, _, _, _, _ = _run_bulk(
        [_mk_order("ORD-1", OrderStatus.PENDING)],
        order_numbers=["ORD-404", "ORD-1"], order_status="cancelled",
        status_remarks="x", confirm_text="cancelled")
    assert (resp.total, resp.updated) == (2, 1)
    assert resp.skipped[0].order_number == "ORD-404"
    assert "not found" in resp.skipped[0].reason.lower()


def test_duplicates_deduped_and_cap_enforced():
    from utils.constants import OrderStatus
    resp, _, _, _, _ = _run_bulk(
        [_mk_order("ORD-1", OrderStatus.PENDING)],
        order_numbers=["ORD-1", "ord-1 ", "ORD-1", "  "], order_status="cancelled",
        status_remarks="x", confirm_text="cancelled")
    assert (resp.total, resp.updated) == (1, 1)
    _expect_400(lambda: _run_bulk(
        [], order_numbers=[f"O-{i}" for i in range(1001)], order_status="cancelled",
        status_remarks="x", confirm_text="cancelled"))


def test_lead_activity_task_scheduled():
    from utils.constants import OrderStatus
    from services import leadService
    _, _, _, _, bt = _run_bulk(
        [_mk_order("ORD-1", OrderStatus.PENDING, lead_id="lead-9")],
        order_numbers=["ORD-1"], order_status="cancelled",
        status_remarks="x", confirm_text="cancelled")
    assert any(t.func is leadService.log_order_status_change for t in bt.tasks)


def test_delivered_target_rejected():
    # Bulk cannot mark orders delivered — out of scope, heavy inventory side effects.
    _expect_400(lambda: _run_bulk(
        [], order_numbers=["ORD-1"], order_status="delivered",
        status_remarks=None, confirm_text="delivered"))


if __name__ == "__main__":
    for _name in sorted(list(globals())):
        if _name.startswith("test_") and callable(globals()[_name]):
            globals()[_name]()
            print(f"PASS {_name}")
