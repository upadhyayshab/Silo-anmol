"""Delivery count / live inventory / live_at on the weekly inventory audit report
(Task T4.2).

`get_audit_report` (routers/v1/inventory_audits.py) is per outlet x product x week
(one row per InventoryAuditItemSchema). This pins three additions to that row/
envelope:

  - `delivery_count`: DELIVERED orders carrying THAT product from THAT outlet,
    strictly after the row's own audit.submitted_at (day granularity -- see
    `_count_deliveries_since`'s docstring for why same-day deliveries are excluded).
    Granularity decision: per (outlet, product), not per outlet -- the row itself
    is per-product, and the reconciliation (system count -> deliveries drawn down
    -> live stock) only coheres at that grain.
  - `live_available_quantity`: the CURRENT `inventory.quantity` for that
    (outlet, product) pair (the same column InventoryAuditService seeds
    system_quantity from) -- None when no inventory row exists, never a
    fabricated 0.
  - `live_at`: one timestamp for the whole report (when it was generated), on the
    response envelope (`WeeklyInventoryAuditReportResponse`), not repeated per row.

Both new lookups are BATCHED (one query each across the whole page's
(outlet, product) pairs) -- precedent: attempt_counts_for/load_events_bulk in
services/order_events_service.py.

DB-backed (sqlite in-memory), same flavor as test_audit_report_state_filter.py --
no live DB is touched.

Run::
    pytest tests/test_audit_report_delivery_and_live_inventory.py -q
"""
import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
import path_setup  # noqa: F401,E402

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from managers.erpManagers import (  # noqa: E402
    OutletSchema, OutletManager,
    ProductCategorySchema, ProductSchema, ProductManager,
    InventoryAuditSchema, InventoryAuditManager,
    InventoryAuditItemSchema, InventoryAuditItemManager,
    InventorySchema, InventoryManager,
    CustomerOrderSchema, CustomerOrderManager,
    OrderItemSchema, OrderItemManager,
)
from utils.constants import AuditStatus, UnitOfMeasure, OrderStatus, CollectionType, PaymentMethod  # noqa: E402
import routers.v1.inventory_audits as IA  # noqa: E402

WEEK_START = date(2026, 7, 11)  # a Saturday
SUBMITTED_AT = datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)  # the Sunday after


def _outlet(uid):
    return OutletSchema(
        uid=uid, outlet_name=f"Outlet {uid}", outlet_code=f"OC-{uid}",
        address="1 Main St", city="Test City", state="Karnataka", pincode="560001",
        phone="9000000000", gstin="29ABCDE1234F1Z5", state_code="KA",
        pan="ABCDE1234F", is_active=True,
    )


def _product(uid, category_id):
    return ProductSchema(
        uid=uid, sku=f"SKU-{uid}", product_name=f"Product {uid}",
        category_id=category_id, hsn_code="12345678", tax_rate=18,
        unit_price=100, cost_price=60, selling_price=90,
        unit_of_measure=UnitOfMeasure.PIECE_UPPER, is_active=True,
    )


def _order(uid, outlet_id, delivered_at, order_status=OrderStatus.DELIVERED):
    return CustomerOrderSchema(
        uid=uid, order_number=f"ORD-{uid}",
        customer_name="Test Customer", customer_phone="9999999999",
        address_line="1 Test Rd", district="Test District", state="Karnataka",
        pincode="560001", telecaller_id="tc-1",
        assigned_outlet_id=outlet_id,
        order_status=order_status,
        collection_type=CollectionType.DOORSTEP,
        payment_method=PaymentMethod.CASH,
        order_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        actual_delivery_date=delivered_at,
        gross_amount=100, total_amount=100,
    )


def _order_item(uid, order_id, product_id):
    return OrderItemSchema(
        uid=uid, order_id=order_id, product_id=product_id,
        quantity=1, unit_price=100, total_price=100, subtotal=100,
    )


class TestAuditReportDeliveryAndLiveInventory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.outlet_mgr = OutletManager(self.engine)
        self.audit_mgr = InventoryAuditManager(self.engine)
        self.item_mgr = InventoryAuditItemManager(self.engine)
        self.inventory_mgr = InventoryManager(self.engine)
        self.order_mgr = CustomerOrderManager(self.engine)
        self.order_item_mgr = OrderItemManager(self.engine)
        await self.outlet_mgr.init_db()  # creates ALL tables (shared BaseSchema.metadata)

        await self.outlet_mgr.create(_outlet("o1"))

        async with self.outlet_mgr.session_factory() as session:
            cat = ProductCategorySchema(uid="cat-1", category_name="Dairy", is_active=True)
            session.add(cat)
            await session.commit()
        await ProductManager(self.engine).create(_product("p1", "cat-1"))
        await ProductManager(self.engine).create(_product("p2", "cat-1"))

        # One audit (submitted) with items for both products.
        await self.audit_mgr.create(InventoryAuditSchema(
            uid="audit-1", outlet_id="o1", audit_date=WEEK_START, week_start=WEEK_START,
            status=AuditStatus.COMPLETED, match_percentage=100, submitted_at=SUBMITTED_AT,
        ))
        await self.item_mgr.create(InventoryAuditItemSchema(
            uid="item-p1", audit_id="audit-1", product_id="p1",
            system_quantity=10, physical_quantity=10,
        ))
        await self.item_mgr.create(InventoryAuditItemSchema(
            uid="item-p2", audit_id="audit-1", product_id="p2",
            system_quantity=5, physical_quantity=5,
        ))

        # Live inventory: a row for p1 (37 on hand), NONE for p2 (unknown stock).
        await self.inventory_mgr.create(InventorySchema(
            uid="inv-p1", product_id="p1", outlet_id="o1", quantity=37,
        ))

        # Deliveries.
        # p1: one BEFORE submitted_at (excluded), two AFTER (counted) -> count == 2.
        await self.order_mgr.create(_order("ord-p1-before", "o1", date(2026, 7, 10)))
        await self.order_item_mgr.create(_order_item("oi-1", "ord-p1-before", "p1"))
        await self.order_mgr.create(_order("ord-p1-after-1", "o1", date(2026, 7, 13)))
        await self.order_item_mgr.create(_order_item("oi-2", "ord-p1-after-1", "p1"))
        await self.order_mgr.create(_order("ord-p1-after-2", "o1", date(2026, 7, 14)))
        await self.order_item_mgr.create(_order_item("oi-3", "ord-p1-after-2", "p1"))
        # A non-DELIVERED order after the cutoff must NOT count.
        await self.order_mgr.create(_order(
            "ord-p1-pending", "o1", date(2026, 7, 13), order_status=OrderStatus.PENDING
        ))
        await self.order_item_mgr.create(_order_item("oi-4", "ord-p1-pending", "p1"))

        # p2: one delivery AFTER submitted_at -> count == 1. Product-scoped, so it
        # must not leak into p1's count (and p1's deliveries must not leak into p2's).
        await self.order_mgr.create(_order("ord-p2-after", "o1", date(2026, 7, 13)))
        await self.order_item_mgr.create(_order_item("oi-5", "ord-p2-after", "p2"))

        self._orig_engine = IA.engine
        self._orig_audit_mgr = IA.audit_manager
        self._orig_item_mgr = IA.audit_item_manager
        IA.engine = self.engine
        IA.audit_manager = self.audit_mgr
        IA.audit_item_manager = self.item_mgr

    async def asyncTearDown(self):
        IA.engine = self._orig_engine
        IA.audit_manager = self._orig_audit_mgr
        IA.audit_item_manager = self._orig_item_mgr

    async def _report_by_product(self, **kwargs):
        result = await IA.get_audit_report(week_start=WEEK_START, **kwargs)
        return result, {item.product_name: item for item in result.items}

    async def test_delivery_count_scoped_to_outlet_and_product(self):
        result, by_product = await self._report_by_product()
        self.assertEqual(by_product["Product p1"].delivery_count, 2)
        self.assertEqual(by_product["Product p2"].delivery_count, 1)

    async def test_deliveries_before_submitted_at_excluded(self):
        # Already implied by the ==2 assertion above (3 DELIVERED p1 orders exist,
        # only 2 are after the cutoff), but assert the excluded one explicitly by
        # tightening the fixture down to just the "before" order.
        _, by_product = await self._report_by_product()
        # 3 DELIVERED p1 orders total (before, after-1, after-2); count must be 2,
        # not 3 -- proves the "before" one was excluded rather than just untested.
        self.assertEqual(by_product["Product p1"].delivery_count, 2)

    async def test_non_delivered_status_not_counted(self):
        # ord-p1-pending is dated after the cutoff but status=PENDING; if it leaked
        # in, p1's count would be 3 instead of 2.
        _, by_product = await self._report_by_product()
        self.assertEqual(by_product["Product p1"].delivery_count, 2)

    async def test_live_available_quantity_known_and_unknown(self):
        _, by_product = await self._report_by_product()
        self.assertEqual(by_product["Product p1"].live_available_quantity, 37)
        self.assertIsNone(by_product["Product p2"].live_available_quantity)

    async def test_live_at_populated_on_envelope(self):
        result, _ = await self._report_by_product()
        self.assertIsNotNone(result.live_at)
        # Sanity: it's "now", not some stale/fixed value.
        now = datetime.now(timezone.utc)
        self.assertLess(abs((now - result.live_at).total_seconds()), 60)

    async def test_no_submitted_at_yields_zero_delivery_count_no_crash(self):
        # A PENDING audit that hasn't been submitted yet -- submitted_at is None.
        await self.audit_mgr.create(InventoryAuditSchema(
            uid="audit-2", outlet_id="o1", week_start=date(2026, 7, 4),
            audit_date=date(2026, 7, 4), status=AuditStatus.PENDING,
        ))
        await self.item_mgr.create(InventoryAuditItemSchema(
            uid="item-p1-w2", audit_id="audit-2", product_id="p1",
            system_quantity=8, physical_quantity=None,
        ))
        result = await IA.get_audit_report(week_start=date(2026, 7, 4))
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].delivery_count, 0)
        self.assertIsNotNone(result.live_at)


if __name__ == "__main__":
    unittest.main()
