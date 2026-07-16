"""T5.1 — booking telecaller becomes Lead Owner (DB-free).

Requirement (Gautam): the last booked activity should capture the telecaller as
Lead Owner. DECISION (stakeholder-confirmed): reassign ONLY when the booker is a
telecaller AND differs from the current owner — a blind reassign-on-every-order
would thrash ownership and fight the auto-assignment pool. See
leadService.reassign_to_booker (calls the existing reassign() so the ASSIGNMENT
activity is logged too, never sets owner_id directly).

Also pins the pre-existing FTU/RTU auto-stage-advance in handle_post_order (T0.1's
stakeholder-confirmed scope: keep FTU/RTU, no new "Booked" stage) as a regression
guard — this task adds a call right after it in the same CRM block, so a reordering
bug there must fail here.

Run::

    python tests/test_booking_telecaller_owner.py     # or: pytest tests/test_booking_telecaller_owner.py
"""
import os
import sys
import asyncio
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

import services.leadService as L               # noqa: E402
from utils.constants import UserRole             # noqa: E402
from utils.crm_enums import LeadActivityType, LeadStage, AssignmentReason  # noqa: E402

# Captured at COLLECTION time (before any test runs), so it is the real, unpatched
# function even though tests/test_assignment_region.py permanently monkeypatches
# `services.leadService.reassign` (module-global, never restored) during its own
# test *execution* later in the same pytest session. reassign_to_booker() calls the
# bare name `reassign` from within leadService's own module namespace, so it would
# otherwise silently pick up that leaked fake when the whole suite runs together.
_REAL_REASSIGN = L.reassign


def run(coro):
    return asyncio.run(coro)


def _lead(uid="lead-1", owner_id="A", stage=None, order_count=0, order_value=None):
    return SimpleNamespace(
        uid=uid, owner_id=owner_id, stage=stage or LeadStage.NEW_LEAD,
        order_count=order_count, order_value=order_value,
    )


class _FakeLeadManager:
    """Fakes LeadManager for both reassign_to_booker/reassign and handle_post_order."""

    def __init__(self, engine):
        pass

    async def fetch(self, lead_id):
        return LEADS.get(lead_id)

    async def update(self, lead_id, patch):
        lead = LEADS[lead_id]
        for k, v in patch.items():
            setattr(lead, k, v)
        return lead


class _FakeLeadActivityManager:
    def __init__(self, engine):
        pass

    async def create(self, schema):
        ACTIVITIES.append(schema)
        return schema


class _FakeAssignmentService:
    def __init__(self):
        self.calls = []

    async def record_assignment(self, engine, lead_id, telecaller_id, *,
                                reason=AssignmentReason.ROUND_ROBIN.value, assigned_by="system"):
        self.calls.append({"lead_id": lead_id, "telecaller_id": telecaller_id,
                           "reason": reason, "assigned_by": assigned_by})
        return SimpleNamespace(lead_id=lead_id, telecaller_id=telecaller_id, reason=reason)


LEADS = {}
ACTIVITIES = []


def _setup(lead):
    """Reset shared fake state and monkeypatch leadService's dependencies."""
    global LEADS, ACTIVITIES
    LEADS = {lead.uid: lead}
    ACTIVITIES = []
    fake_assignment = _FakeAssignmentService()

    orig = (L.LeadManager, L.LeadActivityManager, L.assignmentService, L._fire_capi, L.reassign)
    L.LeadManager = _FakeLeadManager
    L.LeadActivityManager = _FakeLeadActivityManager
    L.assignmentService = fake_assignment
    L._fire_capi = lambda *a, **kw: None  # no Meta CAPI network calls in tests
    L.reassign = _REAL_REASSIGN  # guard against another file's leaked monkeypatch (see note above)

    def _restore():
        L.LeadManager, L.LeadActivityManager, L.assignmentService, L._fire_capi, L.reassign = orig

    return fake_assignment, _restore


# --- Case 1: booking telecaller B differs from current owner A -> reassigned -----

def test_reassign_new_owner_on_telecaller_booking():
    lead = _lead(uid="lead-1", owner_id="A")
    fake_assignment, restore = _setup(lead)
    try:
        run(L.reassign_to_booker("E", "lead-1", "B", UserRole.TELECALLER))
        assert LEADS["lead-1"].owner_id == "B", "owner must move to the booking telecaller"
        assert fake_assignment.calls and fake_assignment.calls[0]["telecaller_id"] == "B"
        assert fake_assignment.calls[0]["reason"] == AssignmentReason.ORDER_BOOKED.value
        # ASSIGNMENT activity must be logged (reassign() calls record_activity for it).
        assignment_logs = [a for a in ACTIVITIES if a.activity_type == LeadActivityType.ASSIGNMENT]
        assert len(assignment_logs) == 1, ACTIVITIES
        assert assignment_logs[0].details["telecaller_id"] == "B"
    finally:
        restore()


def test_reassign_works_for_agency_telecaller_too():
    # AGENCY_TELECALLER is pooled identically to TELECALLER for lead routing/ownership.
    lead = _lead(uid="lead-1", owner_id="A")
    fake_assignment, restore = _setup(lead)
    try:
        run(L.reassign_to_booker("E", "lead-1", "B", UserRole.AGENCY_TELECALLER))
        assert LEADS["lead-1"].owner_id == "B"
    finally:
        restore()


# --- Case 2: booking by the CURRENT owner -> no-op --------------------------------

def test_no_reassign_when_booker_is_current_owner():
    lead = _lead(uid="lead-1", owner_id="A")
    fake_assignment, restore = _setup(lead)
    try:
        run(L.reassign_to_booker("E", "lead-1", "A", UserRole.TELECALLER))
        assert LEADS["lead-1"].owner_id == "A", "no-op: owner must be unchanged"
        assert fake_assignment.calls == [], "must not touch the assignment table"
        assert ACTIVITIES == [], "must not log a spurious ASSIGNMENT activity"
    finally:
        restore()


# --- Case 3: non-telecaller / system booker -> owner unchanged --------------------

def test_no_reassign_for_non_telecaller_booker():
    lead = _lead(uid="lead-1", owner_id="A")
    fake_assignment, restore = _setup(lead)
    try:
        for role in (UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.OUTLET_MANAGER, None):
            run(L.reassign_to_booker("E", "lead-1", "B", role))
        assert LEADS["lead-1"].owner_id == "A"
        assert fake_assignment.calls == []
        assert ACTIVITIES == []
    finally:
        restore()


def test_no_reassign_when_no_booker_id():
    # The store-order path has no telecaller at all (booker_id=None) -> must skip
    # before even fetching the lead.
    lead = _lead(uid="lead-1", owner_id="A")
    fake_assignment, restore = _setup(lead)
    try:
        run(L.reassign_to_booker("E", "lead-1", None, UserRole.TELECALLER))
        assert LEADS["lead-1"].owner_id == "A"
        assert fake_assignment.calls == []
    finally:
        restore()


def test_no_reassign_when_lead_missing():
    lead = _lead(uid="lead-1", owner_id="A")
    fake_assignment, restore = _setup(lead)
    try:
        run(L.reassign_to_booker("E", "does-not-exist", "B", UserRole.TELECALLER))
        assert fake_assignment.calls == []
    finally:
        restore()


# --- Case 5: FTU/RTU auto-stage-advance regression guard (handle_post_order) ------

def test_handle_post_order_stage_ftu_then_rtu():
    lead = _lead(uid="lead-1", owner_id="A", stage=LeadStage.ENGAGED, order_count=0, order_value=None)
    _, restore = _setup(lead)
    try:
        run(L.handle_post_order("E", "lead-1", Decimal("500.00")))
        assert LEADS["lead-1"].stage == LeadStage.FTU, "first order must advance to FTU"
        assert LEADS["lead-1"].order_count == 1
        assert LEADS["lead-1"].order_value == Decimal("500.00")

        run(L.handle_post_order("E", "lead-1", Decimal("300.00")))
        assert LEADS["lead-1"].stage == LeadStage.RTU, "second (repeat) order must advance to RTU"
        assert LEADS["lead-1"].order_count == 2
        assert LEADS["lead-1"].order_value == Decimal("800.00")
    finally:
        restore()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all booking-telecaller-owner checks passed")
