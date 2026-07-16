"""attempt_count folded onto GET /orders rows (Task T2.2). Both the SA export
(superAdmin/apis/orderManagement.js fetchOrders) and the outlet order view
(outlet/apis/outletService.js getOrders) read this same endpoint. Batching is via
order_events_service.attempt_counts_for -> ONE delivery_tracking IN-query grouped in
Python + fold_order_state per group — never one fold query per order. Run:
pytest tests/test_orders_attempt_count.py  OR  python tests/test_orders_attempt_count.py"""
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
    """Mimics DeliveryTrackingManager: fetch_all(filters={"order_id": [...]}) returns every
    row in one shot regardless of the ids filter (tests only ever build the rows they need),
    so counting calls to fetch_all proves a batched query instead of one-per-order."""
    def __init__(self, rows=None):
        self.rows = rows or []
        self.fetch_all_calls = 0

    async def fetch_all(self, limit=0, offset=0, filters=None, sorts=None, **kw):
        self.fetch_all_calls += 1
        return SimpleNamespace(items=list(self.rows), count=len(self.rows))

    async def latest_by_order(self, order_ids):
        return {}


class FakeOrderManager:
    """Mimics CustomerOrderManager.fetch_all(...) -> object with .model_dump()."""
    def __init__(self, order_dicts):
        self._orders = order_dicts

    async def fetch_all(self, **kw):
        orders = self._orders
        return SimpleNamespace(model_dump=lambda: {"items": [dict(o) for o in orders], "count": len(orders)})


def _disposition(order_id, i, status="customer_not_available"):
    return SimpleNamespace(order_id=order_id, event_type="RIDER_DISPOSITION", status_changed_to=status,
                           created_at=datetime(2026, 7, 1, tzinfo=timezone.utc), payload=None)


def test_attempt_counts_for_batches_one_query_no_n_plus_one():
    """N non-delivered dispositions on one order -> attempt_count == N; an order with no
    events at all is simply absent (caller defaults to 0); exactly ONE delivery_tracking
    query is issued for the whole batch of order_ids, not one per order."""
    import services.order_events_service as SVC
    rows = [_disposition("o1", i) for i in range(3)]
    ftrack = FakeTracking(rows=rows)
    orig = SVC.tracking_manager
    SVC.tracking_manager = ftrack
    try:
        counts = asyncio.run(SVC.attempt_counts_for(["o1", "o2", "o3"]))
    finally:
        SVC.tracking_manager = orig
    assert counts.get("o1") == 3
    assert counts.get("o2", 0) == 0            # no events -> fold of empty -> 0, no crash
    assert counts.get("o3", 0) == 0
    assert ftrack.fetch_all_calls == 1          # ONE batched query, not N
    print("OK: test_attempt_counts_for_batches_one_query_no_n_plus_one")


def test_attempt_counts_for_empty_order_ids_short_circuits():
    """No DB round-trip at all when there are no order_ids (e.g. an empty page)."""
    import services.order_events_service as SVC
    ftrack = FakeTracking(rows=[_disposition("o1", 0)])
    orig = SVC.tracking_manager
    SVC.tracking_manager = ftrack
    try:
        counts = asyncio.run(SVC.attempt_counts_for([]))
    finally:
        SVC.tracking_manager = orig
    assert counts == {}
    assert ftrack.fetch_all_calls == 0
    print("OK: test_attempt_counts_for_empty_order_ids_short_circuits")


def test_get_orders_attaches_attempt_count_with_and_without_events():
    """End-to-end through GET /orders (routers.v1.orders.get_orders), the endpoint that
    feeds both the SA export and the outlet order view: an order with 3 non-delivered
    dispositions shows attempt_count == 3 on its row; an order with none shows 0."""
    import routers.v1.orders as O
    import services.order_events_service as SVC
    from utils.auth import AuthContext

    order_rows = [
        {"uid": "o1", "order_number": "ORD-1", "order_status": "attempted"},
        {"uid": "o2", "order_number": "ORD-2", "order_status": "pending"},
    ]
    fom = FakeOrderManager(order_rows)
    tracking_rows = [_disposition("o1", i) for i in range(3)]   # o2 has no events
    ftrack = FakeTracking(rows=tracking_rows)

    orig = (O.order_manager, O.tracking_manager, SVC.tracking_manager)
    O.order_manager = fom
    O.tracking_manager = ftrack
    SVC.tracking_manager = ftrack
    try:
        ctx = AuthContext(user_id="super", role="SUPER_ADMIN", scope_level="GLOBAL")
        resp = asyncio.run(O.get_orders(dynamic_filters={}, sorts=[], ctx=ctx))
    finally:
        O.order_manager, O.tracking_manager, SVC.tracking_manager = orig

    by_uid = {it["uid"]: it for it in resp["items"]}
    assert by_uid["o1"]["attempt_count"] == 3
    assert by_uid["o2"]["attempt_count"] == 0
    print("OK: test_get_orders_attaches_attempt_count_with_and_without_events")


if __name__ == "__main__":
    test_attempt_counts_for_batches_one_query_no_n_plus_one()
    test_attempt_counts_for_empty_order_ids_short_circuits()
    test_get_orders_attaches_attempt_count_with_and_without_events()
    print("All tests passed.")
