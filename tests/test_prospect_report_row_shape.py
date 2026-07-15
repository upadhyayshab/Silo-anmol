"""Prospect report row shape (Task 4): owner name/email only for the owner;
every other detail column — including phone — belongs to the lead.

Before this fix, `email`/`phone` were both the *owner's* (telecaller's) contact
info, which is misleading in a "lead report". Now: `owner` (name) and
`owner_email` stay the owner's; `phone` becomes the lead's mobile; `lead_name`
is added. DB-free — crmReportService.LeadManager is replaced with a fake whose
session.execute() returns canned rows for the batched queries the service runs
in sequence (leads, owners, activities, orders, outlets, delivery, qty). Mirrors
the fake-session/monkeypatch style of tests/test_state_filter.py and
tests/test_orders_scope.py. Run::

    python tests/test_prospect_report_row_shape.py
    # or: pytest tests/test_prospect_report_row_shape.py
"""
import os
import sys
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:  # path_setup prints a checkmark banner; keep it from blowing up a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports


def _lead():
    return SimpleNamespace(
        uid="lead_1",
        lead_number="P-0001",
        first_name="Ravi",
        last_name="Kumar",
        mobile="9876500000",
        owner_id="owner_1",
        stage=None,
        source=None,
        state="Karnataka",
        created_at=datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc),
    )


class _FakeResult:
    """Mimics the subset of SQLAlchemy's Result used by prospect_report:
    .scalars().all() for the leads query, .scalar_one() for the count, and
    plain .all() for the row-tuple queries."""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)

    def scalar_one(self):
        return self._rows

    def all(self):
        return self._rows


class _FakeSession:
    """Returns canned results in the exact order prospect_report issues
    session.execute() calls: count, leads, owners, activities, orders,
    outlets, delivery-people, quantity."""

    def __init__(self, plan):
        self._plan = list(plan)

    async def execute(self, *_a, **_kw):
        return self._plan.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeLeadManager:
    """Stands in for crmReportService.LeadManager(engine) — only session_factory
    is used by prospect_report."""

    def __init__(self, session):
        self._session = session

    def session_factory(self):
        return self._session


def _run_report(lead):
    import services.crmReportService as R

    session = _FakeSession([
        _FakeResult(1),                              # total count
        _FakeResult([lead]),                          # leads
        _FakeResult([("owner_1", "Asha Rao", "asha@example.com")]),  # owners
        _FakeResult([]),                               # call activities
        _FakeResult([]),                               # orders
        _FakeResult([]),                               # outlets (skipped: no outlet_ids, but keep spare)
        _FakeResult([]),                               # delivery people (spare)
        _FakeResult([]),                               # quantity group-by
    ])

    orig = R.LeadManager
    R.LeadManager = lambda engine: _FakeLeadManager(session)
    try:
        rows, total = asyncio.run(R.prospect_report(engine=None))
    finally:
        R.LeadManager = orig
    return rows, total


def _order(gross, discount, total):
    """A minimal customer_order fake — only the fields prospect_report's order
    rollup loop touches (uid deliberately omitted; not read by the loop)."""
    return SimpleNamespace(
        lead_id="lead_1",
        gross_amount=gross,
        discount_applied=discount,
        total_amount=total,
        order_status=None,
        payment_method=None,
        assigned_outlet_id=None,
        delivery_person_id=None,
        status_remarks=None,
    )


def _run_report_with_orders(lead, orders, from_date=None, to_date=None):
    import services.crmReportService as R

    session = _FakeSession([
        _FakeResult(1),                              # total count
        _FakeResult([lead]),                          # leads
        _FakeResult([("owner_1", "Asha Rao", "asha@example.com")]),  # owners
        _FakeResult([]),                               # call activities
        _FakeResult(orders),                           # orders
        _FakeResult([]),                               # outlets
        _FakeResult([]),                               # delivery people
        _FakeResult([]),                               # quantity group-by
    ])

    orig = R.LeadManager
    R.LeadManager = lambda engine: _FakeLeadManager(session)
    try:
        rows, total = asyncio.run(R.prospect_report(engine=None, from_date=from_date, to_date=to_date))
    finally:
        R.LeadManager = orig
    return rows, total


def test_row_has_owner_and_owner_email_and_lead_name():
    rows, _ = _run_report(_lead())
    assert len(rows) == 1
    row = rows[0]
    assert row["owner"] == "Asha Rao"
    assert row["owner_email"] == "asha@example.com"
    assert row["lead_name"] == "Ravi Kumar"


def test_phone_is_the_leads_mobile_not_the_owners():
    rows, _ = _run_report(_lead())
    row = rows[0]
    assert row["phone"] == "9876500000"        # the lead's mobile
    assert row["phone"] != row["owner_email"]  # sanity: distinct owner/lead fields


def test_email_key_no_longer_present():
    rows, _ = _run_report(_lead())
    row = rows[0]
    assert "email" not in row, "the bare 'email' key must be renamed to owner_email"


def test_row_has_created_at_iso():
    rows, _ = _run_report(_lead())
    assert rows[0]["created_at"] == "2026-07-04T12:00:00+00:00"  # raw UTC ISO; UI formats to IST


def test_booked_rev_is_gross_minus_discount_over_the_window():
    """booked_rev = SUM(gross_amount - discount_applied) — the Daily Revenue tab's
    'placed' formula (reports.py `placed` CTE), NOT total_amount (that's `net`).
    avg_booked_rev_per_day divides by the inclusive from/to window's day count."""
    from datetime import date
    orders = [_order(1000, 100, 950), _order(2000, 200, 1900)]  # G=3000, D=300, N=1900+950 net
    rows, _ = _run_report_with_orders(
        _lead(), orders, from_date=date(2026, 7, 1), to_date=date(2026, 7, 5))  # 5-day window
    row = rows[0]
    assert row["booked_rev"] == 2700.0            # round(3000 - 300, 2)
    assert row["avg_booked_rev_per_day"] == 540.0  # round(2700 / 5, 2)
    assert row["net"] == 2850.0                   # untouched: still sum(total_amount)


def test_avg_booked_rev_per_day_defaults_to_total_when_no_window():
    orders = [_order(1000, 100, 950)]
    rows, _ = _run_report_with_orders(_lead(), orders)  # no from_date/to_date -> _pivot_days == 1
    row = rows[0]
    assert row["booked_rev"] == 900.0
    assert row["avg_booked_rev_per_day"] == 900.0  # 900 / 1


if __name__ == "__main__":
    test_row_has_owner_and_owner_email_and_lead_name()
    test_phone_is_the_leads_mobile_not_the_owners()
    test_email_key_no_longer_present()
    test_row_has_created_at_iso()
    test_booked_rev_is_gross_minus_discount_over_the_window()
    test_avg_booked_rev_per_day_defaults_to_total_when_no_window()
    print("OK")
