"""Call-log report (Task B2) — cross-lead call-log list for SA/AA.

DB-free — mirrors the fake-session style of tests/test_prospect_report_row_shape.py:
crmReportService.LeadManager is replaced with a fake whose session.execute() returns
canned rows for the two batched queries call_log_activities issues in sequence
(activities-joined-to-leads, then owner names) AND records the compiled query so the
scope/filter conds it builds via the SHARED `_lead_filter_conds`/`_day_bounds` helpers
can be asserted without a live DB. `_report_scope_owner` itself (GLOBAL -> None,
AGENCY -> roster list, other -> own uid) is already pinned in tests/test_agency.py's
test_report_scope_owner — this file only checks call_log_activities forwards that
value into the right WHERE clause. Run::

    python tests/test_call_log_report.py
    # or: pytest tests/test_call_log_report.py
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


def _activity(uid="act_1", lead_id="lead_1", outcome="answered", direction="inbound",
             disposition=None, duration=42, created_at=None):
    return SimpleNamespace(
        uid=uid,
        lead_id=lead_id,
        outcome=outcome,
        details={"direction": direction, "disposition": disposition, "duration_seconds": duration},
        created_at=created_at or datetime(2026, 7, 10, 9, 30, 0, tzinfo=timezone.utc),
    )


def _lead(uid="lead_1", owner_id="owner_1"):
    return SimpleNamespace(
        uid=uid, lead_number="P-0001", first_name="Ravi", last_name="Kumar", owner_id=owner_id,
    )


class _FakeResult:
    """Mimics the subset of SQLAlchemy's Result used by call_log_activities:
    plain .all() for both the activity+lead join and the owner-names query."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Returns canned results in the exact order call_log_activities issues
    session.execute() calls (join query, owner-names query) and records every
    query object so its compiled WHERE clause can be inspected."""

    def __init__(self, plan):
        self._plan = list(plan)
        self.queries = []

    async def execute(self, query, *_a, **_kw):
        self.queries.append(query)
        return self._plan.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeLeadManager:
    """Stands in for crmReportService.LeadManager(engine) — only session_factory
    is used by call_log_activities."""

    def __init__(self, session):
        self._session = session

    def session_factory(self):
        return self._session


def _run(plan, **kwargs):
    import services.crmReportService as R

    session = _FakeSession(plan)
    orig = R.LeadManager
    R.LeadManager = lambda engine: _FakeLeadManager(session)
    try:
        rows, truncated = asyncio.run(R.call_log_activities(engine=None, **kwargs))
    finally:
        R.LeadManager = orig
    return rows, truncated, session


def _compiled(query):
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def _where_clause(query):
    """The WHERE clause only — the SELECT column list always lists every column
    (including leads.owner_id), so scope assertions must look past it."""
    return _compiled(query).split("WHERE", 1)[1]


def _own_call():
    return _run(
        [_FakeResult([(_activity(), _lead())]),
         _FakeResult([("owner_1", "Asha Rao", "asha@example.com")])],
    )


def test_row_shape():
    rows, truncated, _session = _own_call()
    assert truncated is False
    assert len(rows) == 1
    assert rows[0] == {
        "activity_id": "act_1",
        "created_at": "2026-07-10T09:30:00+00:00",
        "lead_id": "lead_1",
        "lead_number": "P-0001",
        "lead_name": "Ravi Kumar",
        "owner_id": "owner_1",
        "owner_name": "Asha Rao",
        "direction": "inbound",
        "outcome": "answered",
        "disposition": "Connected",   # no explicit `disposition` in details -> derived from outcome
        "duration_seconds": 42,
    }


def test_default_activity_type_filters_call_log():
    _rows, _t, session = _run([_FakeResult([]), _FakeResult([])])
    where = _where_clause(session.queries[0])
    assert "lead_activities.activity_type = 'call_log'" in where


def test_direction_filter_compares_details_json_path():
    _rows, _t, session = _run([_FakeResult([]), _FakeResult([])], direction="inbound")
    where = _where_clause(session.queries[0])
    assert "details" in where and "direction" in where and "inbound" in where


def test_outcome_filter_applied():
    _rows, _t, session = _run([_FakeResult([]), _FakeResult([])], outcome="busy")
    where = _where_clause(session.queries[0])
    assert "lead_activities.outcome = 'busy'" in where


def test_scope_owner_id_list_scopes_to_agency_roster():
    """AGENCY-admin context (a list of roster owner ids from _report_scope_owner)
    restricts the join to those owners' leads only."""
    _rows, _t, session = _run([_FakeResult([]), _FakeResult([])], scope_owner_id=["t1", "t2"])
    where = _where_clause(session.queries[0])
    assert "leads.owner_id IN ('t1', 't2')" in where


def test_scope_owner_id_single_scopes_to_own_leads_only():
    """A lower-role (e.g. telecaller) context passes its own uid -> sees only its
    own leads' call rows."""
    _rows, _t, session = _run([_FakeResult([]), _FakeResult([])], scope_owner_id="tc1")
    where = _where_clause(session.queries[0])
    assert "leads.owner_id = 'tc1'" in where


def test_scope_owner_id_none_is_unscoped_like_sa():
    """SA/GLOBAL context (_report_scope_owner returns None) applies no owner
    restriction at all -> sees every lead's call rows."""
    _rows, _t, session = _run([_FakeResult([]), _FakeResult([])], scope_owner_id=None)
    where = _where_clause(session.queries[0])
    assert "leads.owner_id" not in where


def test_owner_ids_filter_narrows_within_scope():
    _rows, _t, session = _run([_FakeResult([]), _FakeResult([])], owner_ids=["o1", "o2"])
    where = _where_clause(session.queries[0])
    assert "leads.owner_id IN ('o1', 'o2')" in where


def test_truncation_flag_when_over_limit():
    acts = [(_activity(uid=f"act_{i}"), _lead()) for i in range(3)]
    rows, truncated, _session = _run(
        [_FakeResult(acts), _FakeResult([("owner_1", "Asha Rao", "asha@example.com")])],
        limit=2,
    )
    assert truncated is True
    assert len(rows) == 2


def test_not_truncated_when_exactly_at_limit():
    acts = [(_activity(uid=f"act_{i}"), _lead()) for i in range(2)]
    rows, truncated, _session = _run(
        [_FakeResult(acts), _FakeResult([("owner_1", "Asha Rao", "asha@example.com")])],
        limit=2,
    )
    assert truncated is False
    assert len(rows) == 2


def test_disposition_prefers_explicit_pick_over_derived():
    a = _activity(outcome="not_answered", direction="outbound", disposition="Call Back")
    rows, _t, _session = _run(
        [_FakeResult([(a, _lead())]),
         _FakeResult([("owner_1", "Asha Rao", "asha@example.com")])],
    )
    assert rows[0]["disposition"] == "Call Back"
    assert rows[0]["direction"] == "outbound"


def test_owner_name_falls_back_to_empty_when_unresolved():
    rows, _t, _session = _run(
        [_FakeResult([(_activity(), _lead())]), _FakeResult([])],  # no owner rows resolved
    )
    assert rows[0]["owner_name"] == ""


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
