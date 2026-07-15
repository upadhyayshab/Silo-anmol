"""Task F: `_order_state_aggregate` (crmReportService.py) — the per-state order rollup
feeding the Prospect Pivot's 4 new rows. Must mirror the Daily-Rev `placed` CTE
(reports.py:1261-1285) EXACTLY: created_at IST-in-window, no order_status filter, no
deleted_at filter, gross-discount SUM computed WITHOUT joining order_items (fan-out
would inflate it), qty summed via a SEPARATE grouped query. Scope: no lead join at all
when scope_owner_id/agency_id are both None (the case that must match Daily-Rev, which
has no owner/agency scoping); a lead join + `_owner_scope_conds` when either is set.

DB-free — mirrors tests/test_call_pivot.py's fake-session style: a fake session records
every compiled query so its WHERE clause / FROM-join shape can be inspected, and returns
canned per-state aggregate rows. Run::

    python tests/test_order_state_aggregate.py
    # or: pytest tests/test_order_state_aggregate.py
"""
import os
import sys
import asyncio
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:  # path_setup prints a checkmark banner; keep it from blowing up a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Returns canned results in call order (orders_q, then qty_q) and records every
    compiled query so its WHERE/JOIN shape can be asserted on."""

    def __init__(self, plan):
        self._plan = list(plan)
        self.queries = []

    async def execute(self, query, *_a, **_kw):
        self.queries.append(query)
        return self._plan.pop(0)


def _compiled(query):
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def _run(plan, **kwargs):
    import services.crmReportService as R

    session = _FakeSession(plan)
    agg = asyncio.run(R._order_state_aggregate(session, **kwargs))
    return agg, session


def test_no_scope_means_no_lead_join_at_all():
    # This is the case that must match Daily-Rev Placed (superadmin/all-agencies):
    # Daily-Rev has no owner/agency scoping, so neither should this query.
    _agg, session = _run([_FakeResult([]), _FakeResult([])])
    for q in session.queries:
        compiled = _compiled(q).lower()
        assert "leads" not in compiled
        assert "owner_id" not in compiled


def test_scope_owner_id_joins_leads_and_filters_owner():
    _agg, session = _run([_FakeResult([]), _FakeResult([])], scope_owner_id="tc1")
    for q in session.queries:
        compiled = _compiled(q)
        assert "leads" in compiled.lower()
        assert "leads.owner_id = 'tc1'" in compiled


def test_agency_id_joins_leads_via_users_subquery():
    _agg, session = _run([_FakeResult([]), _FakeResult([])], agency_id="ag1")
    for q in session.queries:
        compiled = _compiled(q).lower()
        assert "leads" in compiled
        assert "agency_id" in compiled


def test_window_gates_created_at_no_status_no_deleted_at_filter():
    _agg, session = _run(
        [_FakeResult([]), _FakeResult([])],
        from_date=date(2026, 7, 1), to_date=date(2026, 7, 5))
    for q in session.queries:
        compiled = _compiled(q).lower()
        assert "created_at" in compiled
        assert "order_status" not in compiled   # placed = ALL orders, no status filter
        assert "deleted_at" not in compiled      # the placed CTE has none either


def test_qty_is_a_separate_query_from_the_gross_discount_sum():
    orders_rows = [("Karnataka", 3, Decimal("4050.00")), ("Punjab", 2, Decimal("1400.00"))]
    qty_rows = [("Karnataka", 10), ("Punjab", 10)]
    agg, session = _run([_FakeResult(orders_rows), _FakeResult(qty_rows)])
    assert len(session.queries) == 2
    assert agg == {
        "Karnataka": {"orders": 3, "booked": 4050.0, "qty": 10},
        "Punjab": {"orders": 2, "booked": 1400.0, "qty": 10},
    }


def test_states_are_labeled_and_additive_across_raw_spellings():
    # Two raw spellings folding to the same _state_label sum together, like the
    # lead-count side does.
    orders_rows = [("karnataka", 1, Decimal("100.00")), (" Karnataka ", 1, Decimal("200.00"))]
    qty_rows = [("karnataka", 2), (" Karnataka ", 3)]
    agg, _session = _run([_FakeResult(orders_rows), _FakeResult(qty_rows)])
    assert agg == {"Karnataka": {"orders": 2, "booked": 300.0, "qty": 5}}


def test_empty_state_folds_to_unknown():
    # _state_label now routes through canon_state (Task T1.2); empty/garbage raw
    # states collapse to "Unknown" rather than "Blank".
    orders_rows = [(None, 1, Decimal("50.00"))]
    agg, _session = _run([_FakeResult(orders_rows), _FakeResult([])])
    assert agg["Unknown"]["orders"] == 1
    assert agg["Unknown"]["booked"] == 50.0


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    print("all pass" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
