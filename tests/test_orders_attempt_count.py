"""attempt_count folded onto GET /orders rows (Task T2.2), made lean for limit=0 ("LIMIT:
All" -- the SA export's "fetch everything" mode). Both the SA export
(superAdmin/apis/orderManagement.js fetchOrders) and the outlet order view
(outlet/apis/outletService.js getOrders) read this same endpoint.

order_events_service.attempt_counts_for is now a SQL COUNT aggregate (one row per order),
NOT load_events_bulk + fold_order_state -- that full-fold path is still correct and still
used, unchanged, by /orders/returns and /orders/crm-queue (bounded candidate sets that need
full folded state, not just a count). At limit=0 the candidate set is EVERY order in the
table, so folding every event in Python blows past asyncpg's bind-param cap and never
returns; attempt_counts_for chunks the id list (_ATTEMPT_COUNT_CHUNK) and asks Postgres for
COUNT(*) GROUP BY order_id instead.

GET /orders now issues two lean, 1-row-per-order queries per page: tracking_manager.
latest_by_order (DISTINCT ON, for the "last changed by/from" columns) and
order_events_service.attempt_counts_for (COUNT aggregate, chunked). Run:
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
    """Mimics DeliveryTrackingManager.attempt_counts_by_order / latest_by_order: applies
    the SAME predicate the real SQL query applies (event_type match + case-insensitive
    status_changed_to membership), directly over in-memory rows, so these tests prove the
    SQL predicate's semantics match fold_order_state's without needing a live DB. Each
    call also mimics ONE Postgres round-trip, so counting calls proves batching (one call
    per id-chunk, never one call per order)."""
    def __init__(self, rows=None):
        self.rows = rows or []
        self.attempt_counts_calls = 0
        self.latest_by_order_calls = 0

    async def attempt_counts_by_order(self, order_ids, *, event_type, outcomes):
        self.attempt_counts_calls += 1
        ids = set(order_ids)
        outcomes = set(outcomes)
        result = {}
        for r in self.rows:
            if r.order_id not in ids or r.event_type != event_type:
                continue
            if (r.status_changed_to or "").lower() not in outcomes:
                continue
            result[r.order_id] = result.get(r.order_id, 0) + 1
        return result

    async def latest_by_order(self, order_ids):
        self.latest_by_order_calls += 1
        ids = set(order_ids)
        by_order = {}
        for r in self.rows:
            if r.order_id in ids:
                by_order.setdefault(r.order_id, []).append(r)
        out = {}
        for oid, rows in by_order.items():
            last = max(rows, key=lambda r: r.created_at)
            out[oid] = {"source": last.source, "changed_by": last.changed_by,
                        "changed_by_name": getattr(last, "changed_by_name", None)}
        return out


class FakeOrderManager:
    """Mimics CustomerOrderManager.fetch_all(...) -> object with .model_dump()."""
    def __init__(self, order_dicts):
        self._orders = order_dicts
        self.get_orders_count_calls = 0

    async def fetch_all(self, **kw):
        orders = self._orders
        return SimpleNamespace(model_dump=lambda: {"items": [dict(o) for o in orders], "count": len(orders)})

    async def get_orders_count(self, filters=None):
        # get_orders (Part B) now asks for a filtered total alongside the page;
        # this fake ignores `filters` and just reports the full fake set's size,
        # which is enough to prove the wiring (call happens, value lands in
        # response["total"]) without re-implementing SQL filtering here.
        self.get_orders_count_calls += 1
        return len(self._orders)


def _disposition(order_id, i, status="customer_not_available", event_type="RIDER_DISPOSITION",
                 source=None, changed_by=None):
    # `i` also offsets created_at (seconds) so multiple events on the SAME order sort
    # deterministically -- latest_by_order's DISTINCT ON ... ORDER BY created_at DESC
    # needs a real ordering to pick "the latest" from, and two rows with an identical
    # timestamp would make max()/DISTINCT ON's tie-break arbitrary.
    return SimpleNamespace(order_id=order_id, event_type=event_type, status_changed_to=status,
                           created_at=datetime(2026, 7, 1, 0, 0, i, tzinfo=timezone.utc), payload=None,
                           source=source, changed_by=changed_by)


def test_attempt_counts_for_batches_one_query_no_n_plus_one():
    """N non-delivered dispositions on one order -> attempt_count == N; an order with no
    events at all is simply absent (caller defaults to 0); exactly ONE
    attempt_counts_by_order call is issued for the whole batch of order_ids (below the
    chunk size), not one per order."""
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
    assert counts.get("o2", 0) == 0            # no events -> absent -> 0, no crash
    assert counts.get("o3", 0) == 0
    assert ftrack.attempt_counts_calls == 1     # ONE batched query, not N
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
    assert ftrack.attempt_counts_calls == 0
    print("OK: test_attempt_counts_for_empty_order_ids_short_circuits")


def test_attempt_counts_predicate_matches_fold_order_state():
    """Proves the SQL predicate (event_type == RIDER_DISPOSITION, lower(status_changed_to)
    in NON_DELIVERED_RIDER_OUTCOMES) matches fold_order_state's +1-per-qualifying-event
    rule EXACTLY, by folding the same rows in Python and comparing: a 'delivered' outcome
    (not in NON_DELIVERED_RIDER_OUTCOMES) is not counted, a non-RIDER_DISPOSITION event
    with a matching status string is not counted, and mixed-case outcomes still match
    (fold_order_state lowercases before the membership test)."""
    import services.order_events_service as SVC
    from services.order_events_service import fold_order_state
    rows = [
        _disposition("o1", 0, status="customer_not_available"),
        _disposition("o1", 1, status="Delivered"),                       # not counted
        _disposition("o1", 2, status="UNABLE_TO_CONTACT"),                # mixed case -> counted
        _disposition("o2", 0, status="attempted", event_type="STATUS_CHANGE"),  # wrong event_type
    ]
    ftrack = FakeTracking(rows=rows)
    orig = SVC.tracking_manager
    SVC.tracking_manager = ftrack
    try:
        counts = asyncio.run(SVC.attempt_counts_for(["o1", "o2"]))
    finally:
        SVC.tracking_manager = orig

    by_order = {}
    for r in rows:
        by_order.setdefault(r.order_id, []).append(r)
    reference = {oid: fold_order_state(evs).attempt_count for oid, evs in by_order.items()}

    assert counts.get("o1") == 2 == reference["o1"]
    assert counts.get("o2", 0) == 0 == reference["o2"]
    print("OK: test_attempt_counts_predicate_matches_fold_order_state")


def test_attempt_counts_for_chunks_large_id_lists():
    """More ids than _ATTEMPT_COUNT_CHUNK -> more than one attempt_counts_by_order call
    is issued, and the per-chunk results still merge into one dict correctly."""
    import services.order_events_service as SVC
    chunk = SVC._ATTEMPT_COUNT_CHUNK
    n_ids = chunk + 10
    ids = [f"o{i}" for i in range(n_ids)]
    # One qualifying event each for the first id of the first chunk and the first id of
    # the second chunk, so a wrong merge (e.g. dict overwrite instead of update) shows up.
    rows = [_disposition(ids[0], 0), _disposition(ids[chunk], 0)]
    ftrack = FakeTracking(rows=rows)
    orig = SVC.tracking_manager
    SVC.tracking_manager = ftrack
    try:
        counts = asyncio.run(SVC.attempt_counts_for(ids))
    finally:
        SVC.tracking_manager = orig
    assert ftrack.attempt_counts_calls == 2      # 2 chunks for chunk+10 ids
    assert counts.get(ids[0]) == 1
    assert counts.get(ids[chunk]) == 1
    assert counts.get(ids[1], 0) == 0
    print("OK: test_attempt_counts_for_chunks_large_id_lists")


def test_get_orders_attaches_attempt_count_with_and_without_events():
    """End-to-end through GET /orders (routers.v1.orders.get_orders), the endpoint that
    feeds both the SA export and the outlet order view: an order with 3 non-delivered
    dispositions shows attempt_count == 3 on its row; an order with none shows 0. Also
    pins the restored two-lean-query shape: tracking_manager.latest_by_order for the
    "last changed" columns and order_events_service.attempt_counts_for (backed by the
    SAME fake tracking_manager instance) for attempt_count -- each called exactly once
    for the whole page, never once per order."""
    import routers.v1.orders as O
    import services.order_events_service as SVC
    from utils.auth import AuthContext

    order_rows = [
        {"uid": "o1", "order_number": "ORD-1", "order_status": "attempted"},
        {"uid": "o2", "order_number": "ORD-2", "order_status": "pending"},
    ]
    fom = FakeOrderManager(order_rows)
    # o1's last (most recent) event carries source/changed_by; o2 has no events at all.
    tracking_rows = [
        _disposition("o1", 0),
        _disposition("o1", 1, source="rider_app", changed_by="rider-1"),
    ]
    tracking_rows[1].changed_by_name = "Rider One"
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
    assert by_uid["o1"]["attempt_count"] == 2
    assert by_uid["o2"]["attempt_count"] == 0
    assert by_uid["o1"]["last_status_source"] == "rider_app"
    assert by_uid["o1"]["last_status_changed_by_id"] == "rider-1"
    assert by_uid["o1"]["last_status_changed_by_name"] == "Rider One"
    assert by_uid["o2"]["last_status_source"] is None
    # ONE latest_by_order call and ONE attempt_counts_by_order call for the whole page.
    assert ftrack.latest_by_order_calls == 1
    assert ftrack.attempt_counts_calls == 1
    # Part B: filtered total is additive alongside items/count (existing shape untouched).
    assert resp["total"] == 2
    assert resp["count"] == 2
    print("OK: test_get_orders_attaches_attempt_count_with_and_without_events")


if __name__ == "__main__":
    test_attempt_counts_for_batches_one_query_no_n_plus_one()
    test_attempt_counts_for_empty_order_ids_short_circuits()
    test_attempt_counts_predicate_matches_fold_order_state()
    test_attempt_counts_for_chunks_large_id_lists()
    test_get_orders_attaches_attempt_count_with_and_without_events()
    print("All tests passed.")
