"""C2 fix: booking an order must not burn the telecaller's daily assignment quota.

`leadService.book_order`'s reassign (AssignmentReason.ORDER_BOOKED) re-owns a lead the
telecaller may not have been assigned, by inserting a fresh `lead_assignments` row
(is_active=True, created_at=now). Before this fix, `assignmentService.
_received_today_counts` (the daily-quota GATE) and `stateLaneService.state_lane_overview`'s
`load_by_owner` (the twin state-lane BAR) both counted that row with no `reason` filter --
so booking a routed inbound lead silently consumed a fresh-lead quota slot, throttling the
best inbound converters.

DB-backed (sqlite in-memory, same flavor as test_audit_report_delivery_and_live_inventory.py)
-- no live DB is touched -- so the real SQL WHERE clause (not a mock) is exercised.

Run::
    pytest tests/test_quota_excludes_order_booked.py -q
"""
import importlib
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
import path_setup  # noqa: F401,E402  — must precede manager imports

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from managers.crmManagers import LeadAssignmentSchema, LeadAssignmentManager  # noqa: E402
from utils.crm_enums import AssignmentReason  # noqa: E402
import services.assignmentService as A  # noqa: E402
import services.stateLaneService as SL  # noqa: E402


class TestQuotaExcludesOrderBooked(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # tests/test_quota_daily_cap.py monkeypatches A._active_telecallers /
        # A._received_today_counts onto this SAME cached module object and never
        # restores them (its own documented order-pollution) -- if it ran earlier in
        # this pytest session, `A._received_today_counts` would still be its last fake
        # stub here, not the real DB-backed function this test needs to exercise.
        # Reload restores the pristine module regardless of run order.
        importlib.reload(A)
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.mgr = LeadAssignmentManager(self.engine)
        await self.mgr.init_db()  # creates ALL tables (shared BaseSchema.metadata)

    async def _assign(self, uid, telecaller_id, reason):
        await self.mgr.create(LeadAssignmentSchema(
            uid=uid, lead_id=f"lead-{uid}", telecaller_id=telecaller_id,
            reason=reason, assigned_by="system", is_active=True,
            created_at=datetime.now(timezone.utc),
        ))

    async def test_order_booked_reassign_does_not_consume_quota(self):
        """A telecaller sitting at quota-1 (4 of a 5-lead daily quota already used by
        genuine fresh/backlog assignments) books an order on a lead they don't own.
        That booking (ORDER_BOOKED) must NOT push their today-count to 5 -- they must
        still be eligible for one more fresh lead. A subsequent GENUINE fresh assignment
        must still count normally."""
        tc = "tc-1"
        for i in range(4):
            await self._assign(f"a{i}", tc, AssignmentReason.ROUND_ROBIN.value)
        counts = await A._received_today_counts(self.engine, [tc])
        self.assertEqual(counts.get(tc, 0), 4)

        # Booking an order on a lead they don't own reassigns it to them (ORDER_BOOKED).
        await self._assign("order-booking", tc, AssignmentReason.ORDER_BOOKED.value)
        counts = await A._received_today_counts(self.engine, [tc])
        self.assertEqual(counts.get(tc, 0), 4)   # unchanged -- the booking didn't burn a slot
        self.assertFalse(A._at_quota(_u(tc, 5), counts.get(tc, 0)))  # still eligible

        # A genuine fresh assignment right after still counts normally.
        await self._assign("a4", tc, AssignmentReason.ROUND_ROBIN.value)
        counts = await A._received_today_counts(self.engine, [tc])
        self.assertEqual(counts.get(tc, 0), 5)
        self.assertTrue(A._at_quota(_u(tc, 5), counts.get(tc, 0)))  # now at the daily cap

    async def test_only_order_booked_rows_excluded_other_reasons_still_count(self):
        """Sanity: the exclusion is scoped to ORDER_BOOKED specifically -- MANUAL,
        INBOUND_CALL_ACCESS etc. still count toward the daily total."""
        tc = "tc-2"
        await self._assign("m1", tc, AssignmentReason.MANUAL.value)
        await self._assign("i1", tc, AssignmentReason.INBOUND_CALL_ACCESS.value)
        await self._assign("o1", tc, AssignmentReason.ORDER_BOOKED.value)
        counts = await A._received_today_counts(self.engine, [tc])
        self.assertEqual(counts.get(tc, 0), 2)   # MANUAL + INBOUND_CALL_ACCESS, not ORDER_BOOKED

    async def test_state_lane_load_by_owner_also_excludes_order_booked(self):
        """Twin fix: the state-lane dashboard's load_by_owner (assign_conds in
        stateLaneService.state_lane_overview) must agree with the quota gate above --
        same ORDER_BOOKED row, same exclusion, so the bar and the cap never disagree."""
        tc = "tc-3"
        await self._assign("f1", tc, AssignmentReason.ROUND_ROBIN.value)
        await self._assign("f2", tc, AssignmentReason.ROUND_ROBIN.value)
        await self._assign("ob1", tc, AssignmentReason.ORDER_BOOKED.value)

        overview = await SL.state_lane_overview(self.engine)
        self.assertEqual(overview["totals"]["load"], 2)   # 2 fresh, ORDER_BOOKED excluded


def _u(uid, quota):
    from types import SimpleNamespace
    return SimpleNamespace(uid=uid, assignment_quota=quota, state="ka")


if __name__ == "__main__":
    unittest.main()
