"""GET /orders/export -- streamed, keyset-paginated CSV export (SCOPE: orders only;
the Manage Leads export is a separate, deferred effort, see the task brief).

Exercises services.orderExportService.stream_orders_csv against FAKES (no real DB,
per the task's hard rule -- .env points at PROD). Mirrors the fake/fixture style of
tests/test_orders_attempt_count.py and tests/test_orders_scope.py: swap the module's
manager singletons for small in-memory fakes that reimplement the real semantics
(keyset ordering + tuple comparison, IN-list filtering) rather than hitting SQL.

Covers:
  * column parity -- the header row mirrors OrderReportsTab.jsx's handleExportCSV
    `columns` array exactly (order, labels, "Attempt Count" included).
  * the keyset boundary bug -- more rows than one batch, INCLUDING rows that share
    the same `created_at` instant (the case a single non-unique sort key mishandles):
    every row appears exactly once, in order, across multiple incremental yields.
  * scope -- filters built by the same `_build_order_filters` GET /orders uses
    correctly exclude a non-global caller's out-of-scope rows.
  * the cap -- MAX_EXPORT_ROWS is honoured with a trailing, visible truncation row
    (never silent truncation).
  * attempt_count / enrichment wiring -- present and correct per row, one enrichment
    call per batch (no N+1).

Run: pytest tests/test_orders_export.py  OR  python tests/test_orders_export.py
"""
import os
import sys
import asyncio
import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402


def _order(uid, created_at, *, outlet_id=None, telecaller_id=None, order_number=None,
           attempt=0, outlet=None, telecaller=None):
    """A CustomerOrderSchema-shaped SimpleNamespace: every attribute _build_row reads."""
    return SimpleNamespace(
        uid=uid,
        created_at=created_at,
        order_number=order_number or f"ORD-{uid}",
        order_date=created_at,
        actual_delivery_date=None,
        updated_at=None,
        order_status="DELIVERED",
        status_remarks="",
        pincode="560001",
        house_no="12", street="MG Road", address_line="Near park", village="",
        post="", hobli="", taluk="", district="Bengaluru", state="Karnataka",
        customer_name="Jane Doe", customer_phone="9800000000",
        payment_method="CASH",
        gross_amount=100, discount_applied=0, total_amount=100, total_commission=5,
        telecaller=telecaller,
        delivery_person=None,
        assigned_outlet=outlet,
        # raw scope columns the fake manager's in-memory filter reads (mirrors what
        # real SQLAlchemy `_filter` would match on assigned_outlet_id / telecaller_id)
        assigned_outlet_id=outlet_id,
        telecaller_id=telecaller_id,
    )


class FakeOrderExportManager:
    """Mimics OrderExportManager.fetch_batch/items_for_orders: applies the SAME
    semantics as the real SQL (keyset tuple comparison on (created_at, uid), equality/
    IN filters) over an in-memory list, so these tests prove the pagination LOOP and
    the filter-application contract without a live DB."""

    def __init__(self, orders, items_by_order=None):
        self.orders = list(orders)
        self.items_by_order = items_by_order or {}
        self.fetch_batch_calls = 0
        self.items_calls = 0
        self.batch_sizes = []

    async def fetch_batch(self, filters, after, limit):
        self.fetch_batch_calls += 1
        self.batch_sizes.append(limit)
        rows = list(self.orders)
        for key, val in (filters or {}).items():
            allowed = val if isinstance(val, list) else [val]
            if key == "assigned_outlet_id":
                rows = [o for o in rows if o.assigned_outlet_id in allowed]
            elif key == "telecaller_id":
                rows = [o for o in rows if o.telecaller_id in allowed]
        rows.sort(key=lambda o: (o.created_at, o.uid))
        if after is not None:
            rows = [o for o in rows if (o.created_at, o.uid) > after]
        return rows[:limit]

    async def items_for_orders(self, order_uids):
        self.items_calls += 1
        ids = set(order_uids)
        return {uid: self.items_by_order[uid] for uid in ids if uid in self.items_by_order}


class FakeLatestTracking:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls = 0

    async def latest_by_order(self, order_ids):
        self.calls += 1
        return {oid: self.rows[oid] for oid in order_ids if oid in self.rows}


class FakeAttemptTracking:
    """Mimics DeliveryTrackingManager.attempt_counts_by_order (order_events_service.
    attempt_counts_for's dependency) -- one call per chunk, no N+1."""

    def __init__(self, counts=None):
        self.counts = counts or {}
        self.calls = 0

    async def attempt_counts_by_order(self, order_ids, *, event_type, outcomes):
        self.calls += 1
        ids = set(order_ids)
        return {oid: n for oid, n in self.counts.items() if oid in ids}


def _consume(gen):
    async def _run():
        chunks = []
        async for chunk in gen:
            chunks.append(chunk)
        return chunks
    return asyncio.run(_run())


def _patch(module, **attrs):
    """Set attrs on module, returning a restore callback."""
    orig = {k: getattr(module, k) for k in attrs}
    for k, v in attrs.items():
        setattr(module, k, v)
    def restore():
        for k, v in orig.items():
            setattr(module, k, v)
    return restore


def test_header_row_matches_frontend_export_columns_exactly():
    """Column set/order/labels must mirror OrderReportsTab.jsx handleExportCSV's
    `columns` array EXACTLY, including 'Attempt Count'."""
    import services.orderExportService as OES

    expected_labels = [
        "Order Number", "Order Date", "Delivery Date", "Updated Date", "Status",
        "Remarks", "Status Updated By", "Status Updated By ID", "Status Source",
        "Order pincode", "Outlet", "Outlet Code", "outlet pincode", "Outlet Status",
        "Cluster", "Order Source", "Customer Name", "Customer Phone", "Full Address",
        "Items", "Quantities", "Total Quantity", "Attempt Count", "Created By",
        "Created By Role", "Collection Type", "Gross Amount", "Discount",
        "Net Amount", "Commission",
    ]
    assert [h for h, _ in OES.EXPORT_COLUMNS] == expected_labels

    restore = _patch(OES, order_export_manager=FakeOrderExportManager([]))
    restore_t = _patch(OES, tracking_manager=FakeLatestTracking())
    import services.order_events_service as SVC
    orig_svc_tracking = SVC.tracking_manager
    SVC.tracking_manager = FakeAttemptTracking()
    try:
        chunks = _consume(OES.stream_orders_csv({}))
    finally:
        restore(); restore_t(); SVC.tracking_manager = orig_svc_tracking

    assert len(chunks) == 1                       # header only, no orders -> no crash
    header = next(csv.reader(io.StringIO(chunks[0])))
    assert header == expected_labels
    print("OK: test_header_row_matches_frontend_export_columns_exactly")


def test_keyset_pagination_no_duplicates_or_gaps_across_batches():
    """The classic keyset bug: >1 batch AND several rows sharing the SAME created_at
    instant (a single non-unique sort key would skip/duplicate at the page boundary
    here). Every order must appear exactly once, in (created_at, uid) order, across
    several incremental yields."""
    import services.orderExportService as OES

    t0 = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 7, 1, 0, 0, 1, tzinfo=timezone.utc)
    # 12 orders: 6 share t0, 6 share t1 -- ties on the primary key force the uid
    # tie-break to matter at every batch boundary (batch size 4 below).
    orders = (
        [_order(f"o{i:02d}", t0) for i in range(6)]
        + [_order(f"o{i:02d}", t1) for i in range(6, 12)]
    )
    fake_mgr = FakeOrderExportManager(orders)
    # Batch size 5 over 12 rows -> 5, 5, 2: the last batch is naturally partial, so
    # exhaustion is detected without a spare confirming round-trip (an exact multiple
    # would need one extra empty fetch to know nothing more is left).
    restore = _patch(OES, order_export_manager=fake_mgr, tracking_manager=FakeLatestTracking(),
                     EXPORT_BATCH_SIZE=5, MAX_EXPORT_ROWS=1000)
    import services.order_events_service as SVC
    orig_svc_tracking = SVC.tracking_manager
    SVC.tracking_manager = FakeAttemptTracking()
    try:
        chunks = _consume(OES.stream_orders_csv({}))
    finally:
        restore(); SVC.tracking_manager = orig_svc_tracking

    assert len(chunks) > 1, "generator must yield incrementally, not one blob"
    assert fake_mgr.fetch_batch_calls == 3          # batches of 5, 5, 2

    rows = list(csv.reader(io.StringIO("".join(chunks))))
    header, data_rows = rows[0], rows[1:]
    order_numbers = [r[0] for r in data_rows]

    expected_order = [o.order_number for o in sorted(orders, key=lambda o: (o.created_at, o.uid))]
    assert order_numbers == expected_order            # exact order, no gaps
    assert len(order_numbers) == len(set(order_numbers)) == 12   # no duplicates
    print("OK: test_keyset_pagination_no_duplicates_or_gaps_across_batches")


def test_scope_excludes_out_of_scope_rows():
    """A non-global caller's export must not include rows outside their scope --
    filters come from the SAME `_build_order_filters` GET /orders uses."""
    import routers.v1.orders as O
    import services.orderExportService as OES
    from utils.auth import AuthContext

    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    orders = [
        _order("in1", t0, outlet_id="outlet-1"),
        _order("in2", t0.replace(second=1), outlet_id="outlet-1"),
        _order("out1", t0.replace(second=2), outlet_id="outlet-2"),
    ]
    fake_mgr = FakeOrderExportManager(orders)
    restore = _patch(OES, order_export_manager=fake_mgr, tracking_manager=FakeLatestTracking())
    import services.order_events_service as SVC
    orig_svc_tracking = SVC.tracking_manager
    SVC.tracking_manager = FakeAttemptTracking()
    try:
        ctx = AuthContext(user_id="mgr-1", role="OUTLET_MANAGER", scope_level="OUTLET", outlet_id="outlet-1")
        filters = asyncio.run(O._build_order_filters(
            ctx, transfer_status=None, telecaller_id=None, outlet_id=None, state=None,
            customer_phone=None, from_date=None, to_date=None, dynamic_filters={},
        ))
        assert filters == {"assigned_outlet_id": "outlet-1"}   # OUTLET scope is DB-free

        chunks = _consume(OES.stream_orders_csv(filters))
    finally:
        restore(); SVC.tracking_manager = orig_svc_tracking

    rows = list(csv.reader(io.StringIO("".join(chunks))))
    order_numbers = {r[0] for r in rows[1:]}
    assert order_numbers == {"ORD-in1", "ORD-in2"}
    assert "ORD-out1" not in order_numbers
    print("OK: test_scope_excludes_out_of_scope_rows")


def test_truncation_row_appears_at_cap():
    """More rows exist than MAX_EXPORT_ROWS -> the stream stops at the cap AND a
    visible truncation row is appended (never silent)."""
    import services.orderExportService as OES

    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    orders = [_order(f"o{i}", t0.replace(second=i)) for i in range(5)]
    fake_mgr = FakeOrderExportManager(orders)
    restore = _patch(OES, order_export_manager=fake_mgr, tracking_manager=FakeLatestTracking(),
                     MAX_EXPORT_ROWS=3, EXPORT_BATCH_SIZE=1000)
    import services.order_events_service as SVC
    orig_svc_tracking = SVC.tracking_manager
    SVC.tracking_manager = FakeAttemptTracking()
    try:
        chunks = _consume(OES.stream_orders_csv({}))
    finally:
        restore(); SVC.tracking_manager = orig_svc_tracking

    rows = list(csv.reader(io.StringIO("".join(chunks))))
    data_rows = rows[1:]
    assert len(data_rows) == 4                 # 3 real rows + 1 truncation row
    assert data_rows[-1][0].startswith("TRUNCATED")
    assert "3" in data_rows[-1][0]              # names the actual cap
    real_order_numbers = [r[0] for r in data_rows[:-1]]
    assert len(real_order_numbers) == 3
    assert len(set(real_order_numbers)) == 3
    print("OK: test_truncation_row_appears_at_cap")


def test_attempt_count_and_enrichment_are_per_batch_not_per_row():
    """attempt_count/last_status_* columns are populated from the SAME per-batch
    enrichment calls GET /orders uses (latest_by_order, attempt_counts_for) -- one
    call per batch, not one per order."""
    import services.orderExportService as OES

    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    orders = [_order(f"o{i}", t0.replace(second=i)) for i in range(3)]
    fake_mgr = FakeOrderExportManager(orders)
    fake_latest = FakeLatestTracking({"o1": {"source": "rider_app", "changed_by": "rider-9",
                                              "changed_by_name": "Rider Nine"}})
    restore = _patch(OES, order_export_manager=fake_mgr, tracking_manager=fake_latest)
    import services.order_events_service as SVC
    orig_svc_tracking = SVC.tracking_manager
    SVC.tracking_manager = FakeAttemptTracking({"o1": 4})
    try:
        chunks = _consume(OES.stream_orders_csv({}))
    finally:
        restore(); SVC.tracking_manager = orig_svc_tracking

    assert fake_latest.calls == 1
    assert SVC.tracking_manager is orig_svc_tracking  # restored cleanly

    rows = list(csv.reader(io.StringIO("".join(chunks))))
    header, data_rows = rows[0], rows[1:]
    by_number = {r[header.index("Order Number")]: r for r in data_rows}

    o1 = by_number["ORD-o1"]
    assert o1[header.index("Attempt Count")] == "4"
    assert o1[header.index("Status Updated By")] == "Rider Nine"
    assert o1[header.index("Status Source")] == "Rider App"     # erp/rider_app/system label

    o0 = by_number["ORD-o0"]
    assert o0[header.index("Attempt Count")] == "0"             # no events -> 0, no crash
    assert o0[header.index("Status Source")] == ""
    print("OK: test_attempt_count_and_enrichment_are_per_batch_not_per_row")


def test_item_columns_are_aggregated_from_batch_items_query():
    """Items / Quantities / Total Quantity come from the per-batch items_for_orders
    query (NOT the one-to-many join), aggregated the same way the frontend's
    aggregated_items reducer did."""
    import services.orderExportService as OES

    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    orders = [_order("o1", t0)]
    items = {"o1": [{"name": "Curd 1L", "quantity": 2}, {"name": "Ghee 500ml", "quantity": 1}]}
    fake_mgr = FakeOrderExportManager(orders, items_by_order=items)
    restore = _patch(OES, order_export_manager=fake_mgr, tracking_manager=FakeLatestTracking())
    import services.order_events_service as SVC
    orig_svc_tracking = SVC.tracking_manager
    SVC.tracking_manager = FakeAttemptTracking()
    try:
        chunks = _consume(OES.stream_orders_csv({}))
    finally:
        restore(); SVC.tracking_manager = orig_svc_tracking

    assert fake_mgr.items_calls == 1
    rows = list(csv.reader(io.StringIO("".join(chunks))))
    header, data = rows[0], rows[1]
    assert data[header.index("Items")] == "Curd 1L, Ghee 500ml"
    assert data[header.index("Quantities")] == "2, 1"
    assert data[header.index("Total Quantity")] == "3"
    print("OK: test_item_columns_are_aggregated_from_batch_items_query")


def test_fmt_ist_date_accepts_plain_date():
    # actual_delivery_date is a DATE column -> arrives as datetime.date, which has no
    # tzinfo (crashed the stream mid-response on 2026-07-17). datetimes still convert.
    import services.orderExportService as OES2
    from datetime import date as _date
    assert OES2._fmt_ist_date(_date(2026, 7, 16)) == "16/07/2026"
    # 20:00 UTC = 01:30 IST next day — datetime path still IST-shifts.
    assert OES2._fmt_ist_date(datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc)) == "17/07/2026"
    assert OES2._fmt_ist_date(None) == ""


if __name__ == "__main__":
    test_fmt_ist_date_accepts_plain_date()
    test_header_row_matches_frontend_export_columns_exactly()
    test_keyset_pagination_no_duplicates_or_gaps_across_batches()
    test_scope_excludes_out_of_scope_rows()
    test_truncation_row_appears_at_cap()
    test_attempt_count_and_enrichment_are_per_batch_not_per_row()
    test_item_columns_are_aggregated_from_batch_items_query()
    print("All tests passed.")
