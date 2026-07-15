"""Inbound/outbound call pivot (Task C1) — stage x call-date pivot of unique lead
inflow. Mirrors tests/test_call_log_report.py's fake-session style (call_direction_pivot
fetches the same activity+lead join call_log_activities does — one session.execute()
call whose compiled WHERE clause can be inspected) and tests/test_state_pivot.py's
assertions on the reused build_state_pivot assembly (columns, Lead to Conv%). DB-free.
Run::

    python tests/test_call_pivot.py
    # or: pytest tests/test_call_pivot.py
"""
import os
import sys
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:  # path_setup prints a checkmark banner; keep it from blowing up a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports

from utils.timeutils import IST  # noqa: E402

# 08:00 UTC -> 13:30 IST, comfortably mid-day so it never crosses an IST day
# boundary in either direction.
DAY1 = datetime(2026, 7, 10, 8, 0, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 7, 11, 8, 0, 0, tzinfo=timezone.utc)
DAY1_ISO = DAY1.astimezone(IST).date().isoformat()
DAY2_ISO = DAY2.astimezone(IST).date().isoformat()


def _activity(uid, lead_id, direction, created_at):
    return SimpleNamespace(
        uid=uid, lead_id=lead_id,
        details={"direction": direction},
        created_at=created_at,
    )


def _lead(uid, stage):
    return SimpleNamespace(uid=uid, stage=stage)


class _FakeResult:
    """Mimics the subset of SQLAlchemy's Result used by call_direction_pivot:
    plain .all() for the activity+lead join."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Returns the same canned rows for the single session.execute() call
    call_direction_pivot issues, and records the query so its compiled WHERE
    clause can be inspected (mirrors tests/test_call_log_report.py)."""

    def __init__(self, rows):
        self._rows = rows
        self.queries = []

    async def execute(self, query, *_a, **_kw):
        self.queries.append(query)
        return _FakeResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeLeadManager:
    """Stands in for crmReportService.LeadManager(engine) — only session_factory
    is used by call_direction_pivot."""

    def __init__(self, session):
        self._session = session

    def session_factory(self):
        return self._session


def _run(rows, **kwargs):
    import services.crmReportService as R

    session = _FakeSession(rows)
    orig = R.LeadManager
    R.LeadManager = lambda engine: _FakeLeadManager(session)
    try:
        pivot = asyncio.run(R.call_direction_pivot(engine=None, **kwargs))
    finally:
        R.LeadManager = orig
    return pivot, session


def _compiled(query):
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def test_direction_and_scope_conds_in_where_clause():
    """direction is compared against the JSON 'direction' path in SQL — this is
    what excludes outbound-only leads for direction=inbound at the DB level (same
    idiom call_log_activities uses for its own direction filter)."""
    _pivot, session = _run([], direction="inbound", scope_owner_id="tc1")
    where = _compiled(session.queries[0]).split("WHERE", 1)[1]
    assert "direction" in where and "'inbound'" in where
    assert "leads.owner_id = 'tc1'" in where


def test_outbound_direction_compares_against_outbound():
    _pivot, session = _run([], direction="outbound")
    where = _compiled(session.queries[0]).split("WHERE", 1)[1]
    assert "'outbound'" in where


def test_distinct_lead_counted_once_per_call_day():
    # lead_1 makes two inbound calls the same IST day -> the (day, stage) cell
    # counts it once, not twice ("unique inflow").
    rows = [
        (_activity("a1", "lead_1", "inbound", DAY1), _lead("lead_1", "FTU")),
        (_activity("a2", "lead_1", "inbound", DAY1), _lead("lead_1", "FTU")),
    ]
    pivot, _session = _run(rows, direction="inbound", from_date=date(2026, 7, 10), to_date=date(2026, 7, 11))
    ftu_row = next(r for r in pivot["rows"] if r["label"] == "FTU")
    assert ftu_row["values"][DAY1_ISO] == 1


def test_columns_are_iso_days_sorted_chronologically_plus_grand_total():
    rows = [
        (_activity("a1", "lead_1", "inbound", DAY1), _lead("lead_1", "FTU")),
        (_activity("a2", "lead_2", "inbound", DAY2), _lead("lead_2", "RTU")),
    ]
    pivot, _session = _run(rows, direction="inbound", from_date=date(2026, 7, 10), to_date=date(2026, 7, 11))
    assert pivot["columns"] == [DAY1_ISO, DAY2_ISO, "Grand Total"]


def test_lead_to_conv_pct_is_ftu_plus_rtu_over_total():
    # 4 unique leads that day: FTU, RTU, Not Reachable x2 -> conv% = 2/4 = 50.0
    rows = [
        (_activity("a1", "lead_1", "inbound", DAY1), _lead("lead_1", "FTU")),
        (_activity("a2", "lead_2", "inbound", DAY1), _lead("lead_2", "RTU")),
        (_activity("a3", "lead_3", "inbound", DAY1), _lead("lead_3", "Not Reachable")),
        (_activity("a4", "lead_4", "inbound", DAY1), _lead("lead_4", "Not Reachable")),
    ]
    pivot, _session = _run(rows, direction="inbound", from_date=date(2026, 7, 10), to_date=date(2026, 7, 10))
    conv = next(r for r in pivot["rows"] if r["label"] == "Lead to Conv%")
    assert conv["values"][DAY1_ISO] == 50.0
    assert conv["values"]["Grand Total"] == 50.0


def test_empty_rows_is_safe():
    pivot, _session = _run([], direction="inbound", from_date=date(2026, 7, 10), to_date=date(2026, 7, 11))
    assert pivot["columns"] == ["Grand Total"]


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
