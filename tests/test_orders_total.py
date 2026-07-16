"""Part B -- GET /orders returns a filtered `total` alongside `items`/`count`, so the
Reports grid can page server-side (Page X of Y) instead of relying on the AG-Grid
client-side pager over whatever `limit` happened to fetch.

Mirrors GET /leads' `total` (crmManagers.LeadManager.search_leads: one COUNT query with
the SAME filters, no joins). CustomerOrderManager.get_orders_count already does exactly
this for the existing /orders-count route: `db.select(db.func.count(CustomerOrderSchema.uid))`
filtered the same way as the list query, with NO joins added -- so it can't be inflated
by the items/product one-to-many join `fetch_all`'s `joins=` argument adds for the list.

These tests use a FAKE order manager (no real DB, per the task's hard rule -- .env
points at PROD) that actually applies filters, so `total` proving to differ from both
`count` (page length) and the full unfiltered set size is a real assertion, not a stub
that always returns the same number regardless of what's asked.

Run: pytest tests/test_orders_total.py  OR  python tests/test_orders_total.py
"""
import os
import sys
import asyncio
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402


def _filtered(order_dicts, filters):
    """Same filter semantics FakeOrderExportManager (test_orders_export.py) uses:
    equality / IN over the flat filter dict the router builds."""
    rows = list(order_dicts)
    for key, val in (filters or {}).items():
        allowed = val if isinstance(val, list) else [val]
        rows = [o for o in rows if o.get(key) in allowed]
    return rows


class FakeOrderManagerWithRealCount:
    """Mimics CustomerOrderManager.fetch_all + get_orders_count against ONE in-memory
    set of order dicts, applying filters identically in both so `total` (unbounded by
    limit/offset) and `count`/`items` (bounded page) are provably consistent with each
    other and with the filters passed in -- not two independently-stubbed numbers."""

    def __init__(self, order_dicts):
        self._orders = order_dicts
        self.get_orders_count_calls = 0
        self.fetch_all_calls = 0

    async def fetch_all(self, *, filters=None, limit=0, offset=0, **kw):
        self.fetch_all_calls += 1
        rows = _filtered(self._orders, filters)
        page = rows[offset: offset + limit] if limit else rows[offset:]
        return SimpleNamespace(model_dump=lambda: {"items": [dict(o) for o in page], "count": len(page)})

    async def get_orders_count(self, filters=None):
        self.get_orders_count_calls += 1
        return len(_filtered(self._orders, filters))


class _NoOpTracking:
    """GET /orders also calls tracking_manager.latest_by_order and
    order_events_service.attempt_counts_for as per-page enrichment (see
    test_orders_attempt_count.py); these tests aren't about that enrichment, but
    without faking it out those calls would fall through to the REAL module-level
    managers and hit the real (prod-pointed) engine -- exactly what the task's
    'never query a real DB' rule forbids. No-op fakes keep these tests fast, safe,
    and focused on `total`."""
    async def latest_by_order(self, order_ids):
        return {}

    async def attempt_counts_by_order(self, order_ids, *, event_type, outcomes):
        return {}


def _make_orders(n, *, outlet_id="outlet-1"):
    return [
        {"uid": f"o{i}", "order_number": f"ORD-{i}", "order_status": "pending", "assigned_outlet_id": outlet_id}
        for i in range(n)
    ]


def test_total_reflects_filters_not_page_size():
    """25 orders exist in outlet-1, page size (limit) is 5 -> `count` is 5 (page length)
    but `total` is 25 (the full filtered set), proving `total` isn't just an alias for
    `count` and isn't the unfiltered table size either."""
    import routers.v1.orders as O
    import services.order_events_service as SVC
    from utils.auth import AuthContext

    orders = _make_orders(25, outlet_id="outlet-1") + _make_orders(10, outlet_id="outlet-2")
    fom = FakeOrderManagerWithRealCount(orders)

    orig = (O.order_manager, O.tracking_manager, SVC.tracking_manager)
    O.order_manager = fom
    O.tracking_manager = _NoOpTracking()
    SVC.tracking_manager = _NoOpTracking()
    try:
        ctx = AuthContext(user_id="super", role="SUPER_ADMIN", scope_level="GLOBAL")
        resp = asyncio.run(O.get_orders(
            outlet_id="outlet-1", limit=5, offset=0, dynamic_filters={}, sorts=[], ctx=ctx,
        ))
    finally:
        O.order_manager, O.tracking_manager, SVC.tracking_manager = orig

    assert resp["count"] == 5                 # page length
    assert resp["total"] == 25                # full filtered count, not 35 (unfiltered) or 5
    assert fom.get_orders_count_calls == 1     # one COUNT query, not one per row
    print("OK: test_total_reflects_filters_not_page_size")


def test_total_paging_matches_across_pages():
    """Same filters, different offset/limit (simulating Next/Prev) -> `total` stays
    constant across pages while `items`/`count` change, which is exactly what a
    'Page X of Y' pager built from `total` needs."""
    import routers.v1.orders as O
    import services.order_events_service as SVC
    from utils.auth import AuthContext

    orders = _make_orders(12, outlet_id="outlet-1")
    fom = FakeOrderManagerWithRealCount(orders)

    orig = (O.order_manager, O.tracking_manager, SVC.tracking_manager)
    O.order_manager = fom
    O.tracking_manager = _NoOpTracking()
    SVC.tracking_manager = _NoOpTracking()
    try:
        ctx = AuthContext(user_id="super", role="SUPER_ADMIN", scope_level="GLOBAL")
        page1 = asyncio.run(O.get_orders(
            outlet_id="outlet-1", limit=5, offset=0, dynamic_filters={}, sorts=[], ctx=ctx,
        ))
        page3 = asyncio.run(O.get_orders(
            outlet_id="outlet-1", limit=5, offset=10, dynamic_filters={}, sorts=[], ctx=ctx,
        ))
    finally:
        O.order_manager, O.tracking_manager, SVC.tracking_manager = orig

    assert page1["total"] == page3["total"] == 12
    assert page1["count"] == 5
    assert page3["count"] == 2                 # last partial page
    print("OK: test_total_paging_matches_across_pages")


def test_existing_consumers_items_and_count_shape_unchanged():
    """Additive change: outlet order views and any other GET /orders caller that only
    reads `items`/`count` keep working untouched -- `total` is a new key, not a
    replacement, and doesn't require limit/offset to be present."""
    import routers.v1.orders as O
    import services.order_events_service as SVC
    from utils.auth import AuthContext

    orders = _make_orders(3, outlet_id="outlet-1")
    fom = FakeOrderManagerWithRealCount(orders)

    orig = (O.order_manager, O.tracking_manager, SVC.tracking_manager)
    O.order_manager = fom
    O.tracking_manager = _NoOpTracking()
    SVC.tracking_manager = _NoOpTracking()
    try:
        ctx = AuthContext(user_id="mgr-1", role="OUTLET_MANAGER", scope_level="OUTLET", outlet_id="outlet-1")
        resp = asyncio.run(O.get_orders(dynamic_filters={}, sorts=[], ctx=ctx))
    finally:
        O.order_manager, O.tracking_manager, SVC.tracking_manager = orig

    assert set(["items", "count", "total"]).issubset(resp.keys())
    assert len(resp["items"]) == resp["count"] == 3
    assert resp["total"] == 3
    print("OK: test_existing_consumers_items_and_count_shape_unchanged")


if __name__ == "__main__":
    test_total_reflects_filters_not_page_size()
    test_total_paging_matches_across_pages()
    test_existing_consumers_items_and_count_shape_unchanged()
    print("All tests passed.")
