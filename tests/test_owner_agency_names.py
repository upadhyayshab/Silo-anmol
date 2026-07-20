"""T2.1 — owner->agency resolution (owner_id -> users.agency_id -> agencies.name).

Covers both surfaces that now carry `agency_name`:
  - leadService.owner_agency_names — the batched helper used by the Manage Leads
    list/query paths (routers/v1/leads.py::_leads_to_responses).
  - crmReportService.agent_performance — the per-owner pivot row.

DB-free: monkeypatches the manager whose `session_factory()` each function uses,
feeding canned rows for the batched query(s) they run in sequence — mirrors
tests/test_prospect_report_row_shape.py's fake-session pattern. Run::

    python tests/test_owner_agency_names.py
    # or: pytest tests/test_owner_agency_names.py
"""
import os
import sys
import asyncio

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
    """Returns canned results in the exact order the function under test issues
    session.execute() calls."""

    def __init__(self, plan):
        self._plan = list(plan)
        self.statements = []  # each execute()'d statement, in call order

    async def execute(self, statement=None, *_a, **_kw):
        self.statements.append(statement)
        return self._plan.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeManager:
    """Stands in for whatever manager the function calls `.session_factory()` on
    — only that method is used by the code under test."""

    def __init__(self, session):
        self._session = session

    def session_factory(self):
        return self._session


# --------------------------------------------------------------------------
# leadService.owner_agency_names — the shared batched helper
# --------------------------------------------------------------------------

def _run_owner_agency_names(rows, owner_ids):
    import services.leadService as S

    session = _FakeSession([_FakeResult(rows)])
    orig = S.UserManager
    S.UserManager = lambda engine: _FakeManager(session)
    try:
        return asyncio.run(S.owner_agency_names(None, owner_ids))
    finally:
        S.UserManager = orig


def test_owner_with_agency_returns_agency_name():
    result = _run_owner_agency_names([("owner_1", "Acme Calling")], ["owner_1"])
    assert result == {"owner_1": "Acme Calling"}


def test_owner_without_agency_returns_none():
    result = _run_owner_agency_names([("owner_2", None)], ["owner_2"])
    assert result == {"owner_2": None}
    assert "owner_2" in result  # None means "resolved, no agency" — not "unresolved"


def test_no_owner_ids_short_circuits_without_hitting_the_db():
    import services.leadService as S

    def _boom(engine):
        raise AssertionError("should not query when there are no owner_ids")

    orig = S.UserManager
    S.UserManager = _boom
    try:
        assert asyncio.run(S.owner_agency_names(None, [])) == {}
        # Falsy entries (None/"") are filtered out before the empty-check too.
        assert asyncio.run(S.owner_agency_names(None, [None, ""])) == {}
    finally:
        S.UserManager = orig


# --------------------------------------------------------------------------
# crmReportService.agent_performance — per-owner row now carries agency_name
# --------------------------------------------------------------------------

def _run_agent_performance(stage_rows, ord_rows, qty_rows, owners_rows):
    import services.crmReportService as R

    session = _FakeSession([
        _FakeResult(stage_rows),   # stage counts per owner
        _FakeResult(ord_rows),     # order rollup per telecaller
        _FakeResult(qty_rows),     # quantity per telecaller
        _FakeResult(owners_rows),  # owner name/email + agency name (one LEFT JOIN)
    ])
    orig = R.LeadManager
    R.LeadManager = lambda engine: _FakeManager(session)
    try:
        return asyncio.run(R.agent_performance(None))
    finally:
        R.LeadManager = orig


def test_agent_performance_row_has_agency_name_for_owner_with_agency():
    rows = _run_agent_performance(
        stage_rows=[("owner_1", "New Lead", 3)],
        ord_rows=[],
        qty_rows=[],
        owners_rows=[("owner_1", "Asha Rao", "asha@example.com", "Acme Calling")],
    )
    assert len(rows) == 1
    assert rows[0]["owner_id"] == "owner_1"
    assert rows[0]["agency_name"] == "Acme Calling"


def test_agent_performance_row_has_none_agency_name_for_owner_without_agency():
    rows = _run_agent_performance(
        stage_rows=[("owner_2", "New Lead", 1)],
        ord_rows=[],
        qty_rows=[],
        owners_rows=[("owner_2", "Ravi Kumar", "ravi@example.com", None)],
    )
    assert len(rows) == 1
    assert rows[0]["agency_name"] is None


def test_agent_performance_order_rollup_is_scoped_to_in_scope_telecallers():
    """Regression: an order created by an out-of-scope telecaller on an in-scope
    lead must NOT leak that telecaller into the pivot. The lead-side scope alone
    doesn't gate the order rollup (it groups by the order's creator), so the ord/
    qty queries must also filter telecaller_id to the scoped owner set. Assert the
    scoped id reaches the compiled SQL of those two queries."""
    import services.crmReportService as R

    session = _FakeSession([_FakeResult([]) for _ in range(4)])
    orig = R.LeadManager
    R.LeadManager = lambda engine: _FakeManager(session)
    try:
        asyncio.run(R.agent_performance(None, scope_owner_id=["rm_agent_x"]))
    finally:
        R.LeadManager = orig

    # statements[0]=stage, [1]=order rollup, [2]=quantity, [3]=owner names.
    # `rm_agent_x` alone isn't enough — it also appears via the lead-owner filter.
    # Assert the telecaller_id itself is gated to the scope (the part that was missing).
    ord_sql = str(session.statements[1].compile(compile_kwargs={"literal_binds": True}))
    qty_sql = str(session.statements[2].compile(compile_kwargs={"literal_binds": True}))
    assert "telecaller_id IN ('rm_agent_x')" in ord_sql, ord_sql
    assert "telecaller_id IN ('rm_agent_x')" in qty_sql, qty_sql


if __name__ == "__main__":
    import inspect
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f()
                print("PASS", n)
            except AssertionError as e:
                fails += 1
                print("FAIL", n, e)
    print("OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
