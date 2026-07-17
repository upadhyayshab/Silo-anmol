"""Inbound/outbound call pivot (Task C1; inbound reshaped into a funnel 2026-07-17).

OUTBOUND is untouched — same stage x call-date pivot as before, same single
session.execute() query, so the ORIGINAL fake-session style (one canned result list,
mirrors tests/test_call_log_report.py) still applies verbatim; those tests just moved
from direction="inbound" to direction="outbound" to prove the outbound code path is
byte-for-byte what inbound used to be.

INBOUND now issues up to TWO session.execute() calls (call+lead join, then an ORDER
activities query scoped to the leads seen in the first) — mirrored with a "plan" fake
session (list of canned results popped in call order), same idiom
tests/test_call_log_report.py uses for call_log_activities' two queries. DB-free. Run::

    python tests/test_call_pivot.py
    # or: pytest tests/test_call_pivot.py
"""
import os
import sys
import asyncio
from datetime import date, datetime, timedelta, timezone
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
# Long before DAY1 — outside the +/-60s fresh epsilon by a wide margin.
EARLIER = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
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


def _call(uid, lead_id, created_at, outcome):
    """An inbound CALL_LOG row as returned by _inbound_call_funnel's join query —
    only the fields the funnel actually reads (lead_id, created_at, outcome)."""
    return SimpleNamespace(uid=uid, lead_id=lead_id, created_at=created_at, outcome=outcome,
                          details={"direction": "inbound"})


class _FakeResult:
    """Mimics the subset of SQLAlchemy's Result used here: plain .all()."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Returns the same canned rows for every session.execute() call (outbound issues
    exactly one), and records each query so its compiled WHERE clause can be
    inspected (mirrors the original test_call_pivot.py)."""

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


class _PlanFakeSession:
    """Returns canned results in the exact order _inbound_call_funnel issues
    session.execute() calls (call+lead join, then — only if the first query found any
    leads — the ORDER-activities query), and records every query object so its
    compiled WHERE clause can be inspected. Same idiom as test_call_log_report.py's
    fake session for call_log_activities' two queries."""

    def __init__(self, plan):
        self._plan = list(plan)
        self.queries = []

    async def execute(self, query, *_a, **_kw):
        self.queries.append(query)
        return _FakeResult(self._plan.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeLeadManager:
    """Stands in for crmReportService.LeadManager(engine) — only session_factory
    is used by call_direction_pivot / _inbound_call_funnel."""

    def __init__(self, session):
        self._session = session

    def session_factory(self):
        return self._session


def _run(rows, **kwargs):
    """Single-query helper (outbound path)."""
    import services.crmReportService as R

    session = _FakeSession(rows)
    orig = R.LeadManager
    R.LeadManager = lambda engine: _FakeLeadManager(session)
    try:
        pivot = asyncio.run(R.call_direction_pivot(engine=None, **kwargs))
    finally:
        R.LeadManager = orig
    return pivot, session


def _run_inbound(plan, **kwargs):
    """Plan-based helper (inbound funnel path) — `plan` is
    [call_join_rows] or [call_join_rows, order_rows] (the second query only fires when
    call_join_rows is non-empty)."""
    import services.crmReportService as R

    session = _PlanFakeSession(plan)
    orig = R.LeadManager
    R.LeadManager = lambda engine: _FakeLeadManager(session)
    try:
        pivot = asyncio.run(R.call_direction_pivot(engine=None, direction="inbound", **kwargs))
    finally:
        R.LeadManager = orig
    return pivot, session


def _compiled(query):
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def _row(pivot, label):
    return next(r for r in pivot["rows"] if r["label"] == label)


# --- OUTBOUND: unchanged (Task C1 stage x call-date pivot) ------------------------

def test_outbound_direction_compares_against_outbound():
    _pivot, session = _run([], direction="outbound")
    where = _compiled(session.queries[0]).split("WHERE", 1)[1]
    assert "'outbound'" in where


def test_outbound_distinct_lead_counted_once_per_call_day():
    # lead_1 makes two outbound calls the same IST day -> the (day, stage) cell
    # counts it once, not twice ("unique inflow").
    rows = [
        (_activity("a1", "lead_1", "outbound", DAY1), _lead("lead_1", "FTU")),
        (_activity("a2", "lead_1", "outbound", DAY1), _lead("lead_1", "FTU")),
    ]
    pivot, _session = _run(rows, direction="outbound", from_date=date(2026, 7, 10), to_date=date(2026, 7, 11))
    ftu_row = _row(pivot, "FTU")
    assert ftu_row["values"][DAY1_ISO] == 1


def test_outbound_columns_are_iso_days_sorted_chronologically_plus_grand_total():
    rows = [
        (_activity("a1", "lead_1", "outbound", DAY1), _lead("lead_1", "FTU")),
        (_activity("a2", "lead_2", "outbound", DAY2), _lead("lead_2", "RTU")),
    ]
    pivot, _session = _run(rows, direction="outbound", from_date=date(2026, 7, 10), to_date=date(2026, 7, 11))
    assert pivot["columns"] == [DAY1_ISO, DAY2_ISO, "Grand Total"]


def test_outbound_lead_to_conv_pct_is_ftu_plus_rtu_over_total():
    # 4 unique leads that day: FTU, RTU, Not Reachable x2 -> conv% = 2/4 = 50.0
    rows = [
        (_activity("a1", "lead_1", "outbound", DAY1), _lead("lead_1", "FTU")),
        (_activity("a2", "lead_2", "outbound", DAY1), _lead("lead_2", "RTU")),
        (_activity("a3", "lead_3", "outbound", DAY1), _lead("lead_3", "Not Reachable")),
        (_activity("a4", "lead_4", "outbound", DAY1), _lead("lead_4", "Not Reachable")),
    ]
    pivot, _session = _run(rows, direction="outbound", from_date=date(2026, 7, 10), to_date=date(2026, 7, 10))
    conv = _row(pivot, "Lead to Conv%")
    assert conv["values"][DAY1_ISO] == 50.0
    assert conv["values"]["Grand Total"] == 50.0


def test_outbound_empty_rows_is_safe():
    pivot, _session = _run([], direction="outbound", from_date=date(2026, 7, 10), to_date=date(2026, 7, 11))
    assert pivot["columns"] == ["Grand Total"]


def test_outbound_no_order_rows_task_f():
    # Task F added 4 order/revenue rows to the STATE pivot (state_stage_pivot) only.
    # call_direction_pivot calls build_state_pivot without order_by_state, so its
    # output must never contain them, even with call/lead rows present.
    rows = [
        (_activity("a1", "lead_1", "outbound", DAY1), _lead("lead_1", "FTU")),
    ]
    pivot, _session = _run(rows, direction="outbound", from_date=date(2026, 7, 10), to_date=date(2026, 7, 11))
    labels = {r["label"] for r in pivot["rows"]}
    assert not ({"No. of Orders", "Total Qty", "Booked Revenue", "Avg Booked Rev/Day"} & labels)


# --- INBOUND: the 2026-07-17 5-row funnel ------------------------------------------

def test_inbound_direction_and_scope_conds_in_where_clause():
    """direction is compared against the JSON 'direction' path in SQL, and the scope
    cond is applied against the joined LeadSchema — same idiom the outbound path (and
    call_log_activities) uses. Empty call rows -> no second (order) query fires."""
    _pivot, session = _run_inbound([[]], scope_owner_id="tc1")
    where = _compiled(session.queries[0]).split("WHERE", 1)[1]
    assert "direction" in where and "'inbound'" in where
    assert "leads.owner_id = 'tc1'" in where
    assert len(session.queries) == 1  # no leads found -> order query skipped


def test_inbound_funnel_fixture_day():
    """Brief's fixture: 5 inbound calls, 2 fresh (created their lead) + 3 deduped
    (matched an existing lead), 3 connected, 1 same-day booking -> 5 / 2 / 3 / 3 / 1."""
    call_rows = [
        (_call("a1", "lead_A", DAY1, "answered"), DAY1),       # fresh (created this instant), connected
        (_call("a2", "lead_B", DAY1, "not_answered"), DAY1),   # fresh, not connected
        (_call("a3", "lead_C", DAY1, "answered"), EARLIER),    # deduped (pre-existing lead), connected
        (_call("a4", "lead_D", DAY1, "busy"), EARLIER),        # deduped, not connected
        (_call("a5", "lead_E", DAY1, "answered"), EARLIER),    # deduped, connected
    ]
    order_rows = [("lead_A", DAY1)]   # lead_A books the same day it called
    pivot, _session = _run_inbound(
        [call_rows, order_rows], from_date=date(2026, 7, 10), to_date=date(2026, 7, 10))

    assert _row(pivot, "Incoming Calls")["values"][DAY1_ISO] == 5
    assert _row(pivot, "Fresh Leads")["values"][DAY1_ISO] == 2
    assert _row(pivot, "Deduped")["values"][DAY1_ISO] == 3
    assert _row(pivot, "Connected")["values"][DAY1_ISO] == 3
    assert _row(pivot, "Orders Booked")["values"][DAY1_ISO] == 1
    # Fresh + Deduped == Incoming Calls, per spec.
    assert (_row(pivot, "Fresh Leads")["values"][DAY1_ISO]
            + _row(pivot, "Deduped")["values"][DAY1_ISO]
            == _row(pivot, "Incoming Calls")["values"][DAY1_ISO])


def test_inbound_repeat_caller_same_day_one_fresh_one_deduped():
    # lead_F created by its FIRST call (created_at == lead.created_at); a second call
    # from the same lead 5 minutes later the same day is a repeat, not a re-creation.
    call_rows = [
        (_call("a1", "lead_F", DAY1, "answered"), DAY1),
        (_call("a2", "lead_F", DAY1 + timedelta(minutes=5), "answered"), DAY1),
    ]
    pivot, _session = _run_inbound(
        [call_rows, []], from_date=date(2026, 7, 10), to_date=date(2026, 7, 10))
    assert _row(pivot, "Incoming Calls")["values"][DAY1_ISO] == 2
    assert _row(pivot, "Fresh Leads")["values"][DAY1_ISO] == 1
    assert _row(pivot, "Deduped")["values"][DAY1_ISO] == 1


def test_inbound_booking_without_same_day_call_not_counted():
    # lead_G is created/calls on DAY1 but books on DAY2 with no call that day ->
    # Orders Booked must NOT count it on DAY2 (same-day funnel rule).
    call_rows = [(_call("a1", "lead_G", DAY1, "answered"), DAY1)]
    order_rows = [("lead_G", DAY2)]
    pivot, _session = _run_inbound(
        [call_rows, order_rows], from_date=date(2026, 7, 10), to_date=date(2026, 7, 11))
    booked = _row(pivot, "Orders Booked")
    assert booked["values"].get(DAY1_ISO, 0) == 0
    assert booked["values"].get(DAY2_ISO, 0) == 0
    assert booked["values"] == {}


def test_inbound_grand_total_sums_across_days():
    call_rows = [
        (_call("a1", "lead_A", DAY1, "answered"), DAY1),
        (_call("a2", "lead_B", DAY2, "answered"), DAY2),
    ]
    pivot, _session = _run_inbound(
        [call_rows, []], from_date=date(2026, 7, 10), to_date=date(2026, 7, 11))
    incoming = _row(pivot, "Incoming Calls")
    assert incoming["values"][DAY1_ISO] == 1
    assert incoming["values"][DAY2_ISO] == 1
    assert incoming["values"]["Grand Total"] == 2
    fresh = _row(pivot, "Fresh Leads")
    assert fresh["values"]["Grand Total"] == 2


def test_inbound_empty_rows_is_safe():
    pivot, session = _run_inbound([[]], from_date=date(2026, 7, 10), to_date=date(2026, 7, 11))
    assert pivot["columns"] == ["Grand Total"]
    assert len(session.queries) == 1  # order query skipped, nothing to look up


def test_inbound_no_stage_rows():
    # Old stage labels (FTU/RTU/etc.) must never leak into the new funnel's rows.
    call_rows = [(_call("a1", "lead_A", DAY1, "answered"), DAY1)]
    pivot, _session = _run_inbound(
        [call_rows, []], from_date=date(2026, 7, 10), to_date=date(2026, 7, 10))
    labels = {r["label"] for r in pivot["rows"]}
    assert labels == {"Incoming Calls", "Fresh Leads", "Deduped", "Connected", "Orders Booked"}


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
