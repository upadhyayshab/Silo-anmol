"""State filter on the weekly inventory audit report + summary (Task T4.1).

`get_audit_report`/`get_audit_summary` (routers/v1/inventory_audits.py) already join
audit -> outlet to resolve outlet names; this pins the new optional `state` query
param, which narrows both to the outlets located in that state. It reuses the same
`utils.auth.outlet_ids_for_state` helper the dashboard/orders/reports state filters
already use (full state name, case-insensitive `$ieq`) rather than `state_code`, so
this test also doubles as a real (non-fake) exercise of that resolver against seeded
outlet rows.

DB-backed (sqlite in-memory, same flavor as tests/test_lead_fb_page_filter.py and
tests/test_attendance.py) -- no live DB is touched. The module-level `engine`,
`audit_manager`, `audit_item_manager` (routers/v1/inventory_audits.py) and
`utils.auth._get_outlet_mgr` are monkeypatched to point at the in-memory engine for
the duration of each test.

Run::
    pytest tests/test_audit_report_state_filter.py -q
"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
import path_setup  # noqa: F401,E402

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from managers.erpManagers import (  # noqa: E402
    OutletSchema, OutletManager,
    ProductCategorySchema, ProductSchema, ProductManager,
    InventoryAuditSchema, InventoryAuditManager,
    InventoryAuditItemSchema, InventoryAuditItemManager,
)
from utils.constants import AuditStatus, UnitOfMeasure  # noqa: E402
from utils import auth as auth_module  # noqa: E402
import routers.v1.inventory_audits as IA  # noqa: E402

WEEK_START = date(2026, 7, 11)  # a Saturday


def _outlet(uid, state, state_code):
    return OutletSchema(
        uid=uid, outlet_name=f"Outlet {uid}", outlet_code=f"OC-{uid}",
        address="1 Main St", city="Test City", state=state, pincode="560001",
        phone="9000000000", gstin="29ABCDE1234F1Z5", state_code=state_code,
        pan="ABCDE1234F", is_active=True,
    )


def _product(uid, category_id):
    return ProductSchema(
        uid=uid, sku=f"SKU-{uid}", product_name=f"Product {uid}",
        category_id=category_id, hsn_code="12345678", tax_rate=18,
        unit_price=100, cost_price=60, selling_price=90,
        unit_of_measure=UnitOfMeasure.PIECE_UPPER, is_active=True,
    )


class TestAuditReportStateFilter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Fresh in-memory sqlite engine per test (DB_HOST unset => sqlite+aiosqlite
        # per app/config.py; matches the rest of the suite's DB-backed tests).
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.outlet_mgr = OutletManager(self.engine)
        self.audit_mgr = InventoryAuditManager(self.engine)
        self.item_mgr = InventoryAuditItemManager(self.engine)
        await self.outlet_mgr.init_db()  # creates ALL tables (shared BaseSchema.metadata)

        # Two outlets in two different states.
        await self.outlet_mgr.create(_outlet("o-ka", "Karnataka", "KA"))
        await self.outlet_mgr.create(_outlet("o-tn", "Tamil Nadu", "TN"))

        # One product, one category.
        async with self.outlet_mgr.session_factory() as session:
            cat = ProductCategorySchema(uid="cat-1", category_name="Dairy", is_active=True)
            session.add(cat)
            await session.commit()
        await ProductManager(self.engine).create(_product("p-1", "cat-1"))

        # One audit + one item per outlet, same cycle -- ka COMPLETED, tn PENDING.
        await self.audit_mgr.create(InventoryAuditSchema(
            uid="audit-ka", outlet_id="o-ka", audit_date=WEEK_START, week_start=WEEK_START,
            status=AuditStatus.COMPLETED, match_percentage=100,
        ))
        await self.audit_mgr.create(InventoryAuditSchema(
            uid="audit-tn", outlet_id="o-tn", audit_date=WEEK_START, week_start=WEEK_START,
            status=AuditStatus.PENDING,
        ))
        await self.item_mgr.create(InventoryAuditItemSchema(
            uid="item-ka", audit_id="audit-ka", product_id="p-1",
            system_quantity=10, physical_quantity=10,
        ))
        await self.item_mgr.create(InventoryAuditItemSchema(
            uid="item-tn", audit_id="audit-tn", product_id="p-1",
            system_quantity=5, physical_quantity=None,
        ))

        # Point the router module + the outlet_ids_for_state resolver at this engine.
        self._orig_engine = IA.engine
        self._orig_audit_mgr = IA.audit_manager
        self._orig_item_mgr = IA.audit_item_manager
        self._orig_get_outlet_mgr = auth_module._get_outlet_mgr
        IA.engine = self.engine
        IA.audit_manager = self.audit_mgr
        IA.audit_item_manager = self.item_mgr
        auth_module._get_outlet_mgr = lambda: self.outlet_mgr

    async def asyncTearDown(self):
        IA.engine = self._orig_engine
        IA.audit_manager = self._orig_audit_mgr
        IA.audit_item_manager = self._orig_item_mgr
        auth_module._get_outlet_mgr = self._orig_get_outlet_mgr

    # ---- get_audit_report ----------------------------------------------------

    async def test_report_no_state_returns_all(self):
        result = await IA.get_audit_report(week_start=WEEK_START)
        outlet_names = {item.outlet_name for item in result.items}
        self.assertEqual(outlet_names, {"Outlet o-ka", "Outlet o-tn"})

    async def test_report_state_filters_to_matching_outlets_only(self):
        result = await IA.get_audit_report(week_start=WEEK_START, state="Karnataka")
        outlet_names = {item.outlet_name for item in result.items}
        self.assertEqual(outlet_names, {"Outlet o-ka"})

    async def test_report_state_case_insensitive_and_no_match_returns_empty(self):
        result = await IA.get_audit_report(week_start=WEEK_START, state="karnataka")
        self.assertEqual({i.outlet_name for i in result.items}, {"Outlet o-ka"})

        empty = await IA.get_audit_report(week_start=WEEK_START, state="Nowhere State")
        self.assertEqual(empty.items, [])

    async def test_report_explicit_outlet_id_wins_over_state(self):
        # outlet_id is more specific than state -- mirrors orders.py/reports.py convention.
        result = await IA.get_audit_report(week_start=WEEK_START, outlet_id="o-tn", state="Karnataka")
        self.assertEqual({i.outlet_name for i in result.items}, {"Outlet o-tn"})

    # ---- get_audit_summary -----------------------------------------------------

    async def test_summary_no_state_counts_all_outlets(self):
        summary = await IA.get_audit_summary(week_start=WEEK_START)
        self.assertEqual(summary.total_active_outlets, 2)
        self.assertEqual(summary.completed_audits, 1)
        pending_ids = {o.outlet_id for o in summary.pending_outlets}
        self.assertEqual(pending_ids, {"o-tn"})

    async def test_summary_state_narrows_denominator_and_lists(self):
        summary = await IA.get_audit_summary(week_start=WEEK_START, state="Karnataka")
        self.assertEqual(summary.total_active_outlets, 1)
        self.assertEqual(summary.completed_audits, 1)
        self.assertEqual(summary.pending_outlets, [])
        self.assertEqual([o.outlet_id for o in summary.completed_outlets], ["o-ka"])

    async def test_summary_state_with_no_outlets_returns_zeroed_summary(self):
        summary = await IA.get_audit_summary(week_start=WEEK_START, state="Nowhere State")
        self.assertEqual(summary.total_active_outlets, 0)
        self.assertEqual(summary.completed_audits, 0)
        self.assertEqual(summary.completed_outlets, [])
        self.assertEqual(summary.pending_outlets, [])


if __name__ == "__main__":
    unittest.main()
