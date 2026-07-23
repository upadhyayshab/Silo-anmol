"""Streamed, keyset-paginated CSV export for GET /orders/export.

SCOPE: orders only -- the Manage Leads export is a separate, deferred effort; this
module doesn't touch it.

Why this exists: the superadmin report's "Export Excel" button built the file
client-side from `reportingOrders` -- the rows already loaded in the browser -- which
meant the grid had to load with `limit=0` ("LIMIT: All") to get everything first.
`SharedBackend/managers/base.py`'s `if limit: query = query.limit(limit)` treats 0 as
falsy, so no LIMIT was ever emitted: every order row got selected, then materialised
through ORM -> Pydantic -> field-mask -> JSON, single-threaded. Measured ~140s and
unbounded memory. This module streams the CSV server-side instead:

- keyset pagination on (created_at, uid), batch 1000 -- never OFFSET, which degrades
  quadratically once you're paging past ~100k rows.
- capped at MAX_EXPORT_ROWS (shared with crmReportService's export) with a trailing
  CSV row so truncation is never silent.
- per-batch enrichment only (tracking_manager.latest_by_order,
  order_events_service.attempt_counts_for, item aggregation) -- never over the whole
  set.

Columns mirror OrderReportsTab.jsx's `handleExportCSV` exactly -- see EXPORT_COLUMNS.
Three of them (Items / Quantities / Total Quantity) need order_items+product data.
Rather than the one-to-many items/product join GET /orders uses (which the brief this
was built from calls out as a row-multiplying join), each batch fetches its own items
with one bounded `WHERE order_id IN (<=1000 uids)` query and aggregates in Python --
cheap, bounded, and doesn't change the main orders query's row cardinality.
"""
import csv
import io
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import sqlalchemy as db
from sqlalchemy.orm import joinedload

from config import get_engine, get_settings
from managers import (
    CustomerOrderSchema, OrderItemSchema, OutletSchema, ProductSchema,
    CustomerOrderManager, DeliveryTrackingManager,
)
from services import order_events_service
from utils.timeutils import IST

settings = get_settings()
engine = get_engine(settings.name)

EXPORT_BATCH_SIZE = 1000

# Reuse the CRM export's hard ceiling -- one "don't OOM the single-task prod box" rule
# for every streamed export, not a second hardcoded number that could drift from it.
from services.crmReportService import MAX_EXPORT_ROWS  # noqa: E402

# (header, key) -- mirrors OrderReportsTab.jsx handleExportCSV's `columns` array
# EXACTLY: same set, same order, same header labels, including "Attempt Count".
EXPORT_COLUMNS: List[Tuple[str, str]] = [
    ("Order Number", "order_number"),
    ("Order UID", "uid"),
    ("Order Date", "order_date"),
    ("Expected Delivery", "expected_delivery_date"),
    ("Delivery Date", "actual_delivery_date"),
    ("Updated Date", "updated_at"),
    ("Status", "order_status"),
    ("Remarks", "status_remarks"),
    ("Status Updated By", "last_status_changed_by_name"),
    ("Status Updated By ID", "last_status_changed_by_id"),
    ("Status Source", "last_status_source"),
    ("Order Pincode", "pincode"),
    ("Outlet", "assigned_outlet.outlet_name"),
    ("Outlet Code", "assigned_outlet.outlet_code"),
    ("Outlet Pincode", "assigned_outlet.pincode"),
    ("Outlet Status", "outlet_status"),
    ("Cluster", "cluster"),
    ("Source", "order_source"),
    ("Order Type", "collection_type"),
    ("Customer Name", "customer_name"),
    ("Customer Phone", "customer_phone"),
    ("Full Address", "full_address"),
    ("Items", "item_names"),
    ("Quantities", "item_quantities"),
    ("Total Quantity", "total_quantity"),
    ("Attempt Count", "attempt_count"),
    ("Created By", "telecaller.full_name"),
    ("Role", "telecaller.role"),
    ("Collection Type", "payment_method"),
    ("Gross Amount", "gross_amount"),
    ("Discount", "discount_applied"),
    ("Net Amount", "total_amount"),
    ("Commission", "total_commission"),
]

def _truncation_row() -> List[str]:
    # Computed on call (not module-load) so it always reflects the *current*
    # MAX_EXPORT_ROWS -- tests patch that module attribute to a small number rather
    # than generating 100k+ fake rows to exercise the cap.
    return [f"TRUNCATED — first {MAX_EXPORT_ROWS} rows only; narrow your filters"] + [""] * (len(EXPORT_COLUMNS) - 1)


_STATUS_SOURCE_LABEL = {"erp": "ERP", "rider_app": "Rider App", "system": "System"}


def _enum_val(value):
    """Enum columns (order_status, payment_method) may come back as the Enum member
    or its raw string depending on the driver/dialect; normalise to the plain value
    the frontend renders."""
    return getattr(value, "value", value)


def _fmt_ist_date(value) -> str:
    """DD/MM/YYYY in IST -- mirrors formatISTDate (src/utils/datetime.js)."""
    if not value:
        return ""
    dt = value
    if not isinstance(dt, datetime):  # plain DATE column (e.g. actual_delivery_date)
        return dt.strftime("%d/%m/%Y")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d/%m/%Y")


def _fmt_ist_datetime(value) -> str:
    """DD/MM/YYYY, HH:MM in IST -- mirrors formatISTDateTime (src/utils/datetime.js)."""
    if not value:
        return ""
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d/%m/%Y, %H:%M")


def _text(value) -> str:
    return "" if value is None else str(value)


class OrderExportManager:
    """Thin seam the streaming loop calls per batch, so tests can swap in a fake that
    mimics keyset semantics over an in-memory list (same style as FakeOrderManager /
    FakeTracking in tests/test_orders_attempt_count.py) without touching a real DB."""

    def __init__(self, engine):
        self._order_manager = CustomerOrderManager(engine)

    async def fetch_batch(
        self,
        filters: Dict[str, Any],
        after: Optional[Tuple[datetime, str]],
        limit: int,
    ) -> List[CustomerOrderSchema]:
        """One page, ordered by (created_at, uid) ascending -- a stable, gap-free key
        even when many rows share the same created_at instant. `after` is the previous
        batch's last (created_at, uid); keyset (WHERE (created_at, uid) > (:c, :u)),
        never OFFSET. Loads telecaller/delivery_person/assigned_outlet(.cluster) via
        joinedload -- all to-one, so no row multiplication -- and deliberately NOT
        items/product (see module docstring)."""
        query = db.select(CustomerOrderSchema).options(
            joinedload(CustomerOrderSchema.telecaller),
            joinedload(CustomerOrderSchema.delivery_person),
            joinedload(CustomerOrderSchema.assigned_outlet).joinedload(OutletSchema.cluster),
        )
        if filters:
            query = await self._order_manager._filter(query, dict(filters), CustomerOrderSchema)  # noqa: SLF001
        if after is not None:
            last_created, last_uid = after
            query = query.where(
                db.tuple_(CustomerOrderSchema.created_at, CustomerOrderSchema.uid)
                > db.tuple_(last_created, last_uid)
            )
        query = query.order_by(
            CustomerOrderSchema.created_at.asc(), CustomerOrderSchema.uid.asc()
        ).limit(limit)
        async with self._order_manager.session_factory() as session:
            result = await session.execute(query)
            return list(result.unique().scalars())

    async def items_for_orders(self, order_uids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """{order_id: [{name, quantity}, ...]} for one batch's order ids via a single
        bounded IN query (<=1000 ids -- the export's batch size) -- not the one-to-many
        join. Empty batch -> no round trip at all."""
        if not order_uids:
            return {}
        query = (
            db.select(OrderItemSchema.order_id, ProductSchema.product_name, OrderItemSchema.quantity)
            .join(ProductSchema, OrderItemSchema.product_id == ProductSchema.uid)
            .where(OrderItemSchema.order_id.in_(order_uids))
            .order_by(OrderItemSchema.order_id, OrderItemSchema.created_at.asc())
        )
        async with self._order_manager.session_factory() as session:
            rows = (await session.execute(query)).all()
        out: Dict[str, List[Dict[str, Any]]] = {}
        for order_id, name, qty in rows:
            out.setdefault(order_id, []).append({"name": name, "quantity": qty})
        return out


order_export_manager = OrderExportManager(engine)
tracking_manager = DeliveryTrackingManager(engine)


def _build_row(order, latest: Dict[str, Any], attempt_count: int, items: List[Dict[str, Any]]) -> List[Any]:
    telecaller = order.telecaller
    outlet = order.assigned_outlet
    cluster = outlet.cluster if outlet is not None else None
    updated_at = order.updated_at or order.created_at
    order_source = "Offline" if (telecaller is not None and telecaller.role == "OUTLET_MANAGER") else "Online"

    address_parts = [
        order.house_no, order.street, order.address_line, order.village, order.post,
        order.hobli, order.taluk, order.district, order.state, order.pincode,
    ]
    full_address = ", ".join(str(p).strip() for p in address_parts if p and str(p).strip() != "")

    item_names = ", ".join(i["name"] or "" for i in items)
    item_quantities = ", ".join(_text(i["quantity"]) for i in items)
    total_quantity = sum((i["quantity"] or 0) for i in items)

    status_source = latest.get("source")
    status_source_label = _STATUS_SOURCE_LABEL.get(status_source, status_source) if status_source else ""

    values = {
        "order_number": order.order_number,
        "uid": order.uid,
        "order_date": _fmt_ist_date(order.order_date),
        "expected_delivery_date": _fmt_ist_date(getattr(order, "expected_delivery_date", None)),
        "actual_delivery_date": _fmt_ist_date(getattr(order, "actual_delivery_date", None)),
        "updated_at": _fmt_ist_datetime(updated_at),
        "order_status": _enum_val(order.order_status),
        "status_remarks": order.status_remarks,
        "last_status_changed_by_name": latest.get("changed_by_name"),
        "last_status_changed_by_id": latest.get("changed_by"),
        "last_status_source": status_source_label,
        "pincode": order.pincode,
        "assigned_outlet.outlet_name": outlet.outlet_name if outlet else "",
        "assigned_outlet.outlet_code": outlet.outlet_code if outlet else "",
        "assigned_outlet.pincode": outlet.pincode if outlet else "",
        "outlet_status": ("Active" if outlet.is_active else "Inactive") if outlet else "",
        "cluster": cluster.name if cluster else "",
        "order_source": order_source,
        "collection_type": _enum_val(getattr(order, "collection_type", None)),
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "full_address": full_address,
        "item_names": item_names,
        "item_quantities": item_quantities,
        "total_quantity": total_quantity,
        "attempt_count": attempt_count,
        "telecaller.full_name": telecaller.full_name if telecaller else "",
        "telecaller.role": telecaller.role if telecaller else "",
        "payment_method": _enum_val(order.payment_method),
        "gross_amount": order.gross_amount,
        "discount_applied": order.discount_applied,
        "total_amount": order.total_amount,
        "total_commission": order.total_commission,
    }
    return [_text(values[key]) for _, key in EXPORT_COLUMNS]


async def stream_orders_csv(filters: Dict[str, Any]) -> AsyncGenerator[str, None]:
    """Async generator: header row, then batch rows, then (if the cap was hit) a
    truncation row. Builds each chunk with the stdlib csv module into an io.StringIO
    that's flushed and truncated per batch -- never accumulates the whole file."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow([header for header, _ in EXPORT_COLUMNS])
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)

    after: Optional[Tuple[datetime, str]] = None
    yielded = 0

    while yielded < MAX_EXPORT_ROWS:
        batch_limit = min(EXPORT_BATCH_SIZE, MAX_EXPORT_ROWS - yielded)
        orders = await order_export_manager.fetch_batch(filters, after, batch_limit)
        if not orders:
            break

        uids = [o.uid for o in orders]
        latest = await tracking_manager.latest_by_order(uids)
        attempt_counts = await order_events_service.attempt_counts_for(uids)
        items_by_order = await order_export_manager.items_for_orders(uids)

        for o in orders:
            row = _build_row(
                o, latest.get(o.uid) or {}, attempt_counts.get(o.uid, 0), items_by_order.get(o.uid) or []
            )
            writer.writerow(row)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        yielded += len(orders)
        last = orders[-1]
        after = (last.created_at, last.uid)

        if len(orders) < batch_limit:
            break  # fewer rows than asked for -> exhausted, nothing left to page

    if yielded >= MAX_EXPORT_ROWS:
        writer.writerow(_truncation_row())
        yield buf.getvalue()
