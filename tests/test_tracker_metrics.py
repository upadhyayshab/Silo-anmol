"""Golden test for the Business Daily Tracker metric contract.

Feeds the ORIGINAL Google Sheet's June-2026 daily arrays (tab "KN June26", Lead Gen
section) into build_grid and asserts the MTD column reproduces the sheet's own totals.

Where the sheet is wrong (defects D2/D3/D4 in the design doc) this test asserts the
CORRECTED value and documents what the sheet printed.

Pure / DB-free. Run with:
    python -m pytest tests/test_tracker_metrics.py -v
"""
import os
import sys
from datetime import date

import pytest

# Import the module directly, NOT via `from services.tracker_metrics import ...`.
# app/services/__init__.py eagerly imports invoice_service -> managers -> SharedBackend,
# which needs a DB layer on sys.path and defeats the point of a pure test. Pointing
# sys.path at app/services/ loads tracker_metrics.py without executing that __init__.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "services"))

from tracker_metrics import (  # noqa: E402
    ROWS, INPUT_KEYS, build_grid, safe_div, week_buckets, weekday_indices,
)

JUNE = [date(2026, 6, d) for d in range(1, 31)]   # Jun 1 2026 is a Monday
TODAY = date(2026, 7, 1)                           # month closed

# ---- verbatim from the sheet, tab "KN June26" -------------------------------
LEADS       = [481,478,452,435,536,761,783,725,648,1014,1150,1213,1029,1305,772,805,895,782,793,858,731,914,791,908,842,841,1098,749,804,728]
BOOKED      = [111,98,105,103,118,141,22,156,144,169,176,204,171,0,187,153,154,97,193,185,46,169,153,245,220,160,224,0,228,176]
CANCELLED   = [85,80,85,82,85,98,10,86,97,101,100,122,101,0,120,91,107,58,133,125,25,108,76,127,93,100,107,0,114,82]
PENDING     = [11,23,24,22,26,24,6,35,31,28,29,38,36,0,34,40,39,17,43,44,12,42,47,76,93,91,114,0,109,101]
DELIVERED   = [31,26,40,32,37,32,12,43,57,59,58,67,48,11,51,50,54,65,46,51,19,50,71,80,84,95,80,25,78,89]
REVENUE     = [110010,109338,116592,113360,118684,149426,26851,147816,166935,174070,190996,202538,181487,0,177394,158697,149495,95151,162947,181529,47288,147412,141140,231486,218574,153056,211568,0,215883,171972]
SPENDS      = [7278,7125,7469,8420,13909,16875,19449,18684,20087,28738,35887,34617,35050,46086,23758,21645,24051,20936,23193,23667,23182,25333,21184,24589,22293,23501,33049,23502,24071,20474]


def _base():
    """Only Lead Gen is populated; D2C and Organic are zero so Overall == Lead Gen."""
    zeros = [0] * 30
    b = {r.id: list(zeros) for r in ROWS if r.source != "derived"}
    b["leadgen.leads_lsq_total"] = LEADS
    b["leadgen.orders_booked"] = BOOKED
    b["leadgen.orders_cancelled"] = CANCELLED
    b["leadgen.orders_pending"] = PENDING
    b["leadgen.orders_delivered"] = DELIVERED
    b["leadgen.revenue_booked"] = REVENUE
    b["leadgen.spends"] = SPENDS
    b["leadgen.revenue_received"] = REVENUE          # stand-in, only used by estimated revenue
    return b


def _row(grid, section, key):
    for s in grid["sections"]:
        if s["key"] == section:
            for r in s["rows"]:
                if r["key"] == key:
                    return r
    raise KeyError(f"{section}.{key}")


def test_catalogue_shape():
    """103 DATA rows. The sheet's 108 labels include 4 section headers + the title."""
    from collections import Counter
    assert len(ROWS) == 103
    assert Counter(r.section for r in ROWS) == {"overall": 18, "leadgen": 34, "d2c": 34, "organic": 17}
    assert len({r.id for r in ROWS}) == 103, "row ids must be unique"
    assert all(r.tooltip for r in ROWS), "every row must carry a tooltip"


def test_input_keys_are_exactly_the_six_hand_entered_metrics():
    """Identity, not a subset check.

    INPUT_KEYS drives `editable` in the grid payload AND the PUT whitelist. A subset
    check stays green if a key is swapped for another valid row id -- which would
    silently make a DERIVED row editable and stop persisting the real one.
    """
    assert INPUT_KEYS == {
        "leadgen.meta_leads", "leadgen.spends",
        "d2c.spends", "d2c.sessions", "d2c.engaged_users", "d2c.add_to_cart",
    }
    by_id = {r.id: r for r in ROWS}
    for key in INPUT_KEYS:
        assert by_id[key].source == "input", f"{key} is {by_id[key].source}, not an input row"
    assert {r.id for r in ROWS if r.source == "input"} == INPUT_KEYS


def test_d2c_row_order_matches_the_sheet():
    """Pre-paid sits between Units Booked and Revenue Booked, not after the order block."""
    d2c = [r.key for r in ROWS if r.section == "d2c"]
    i = d2c.index("orders_booked")
    assert d2c[i:i + 4] == ["orders_booked", "units_booked", "prepaid_booked_orders", "revenue_booked"]


def test_mtd_reproduces_the_sheet():
    grid = build_grid(JUNE, _base(), TODAY)
    assert _row(grid, "leadgen", "leads_lsq_total")["mtd"] == 24321
    assert _row(grid, "leadgen", "orders_booked")["mtd"] == 4308
    assert _row(grid, "leadgen", "orders_cancelled")["mtd"] == 2598
    assert _row(grid, "leadgen", "orders_pending")["mtd"] == 1235
    assert _row(grid, "leadgen", "orders_delivered")["mtd"] == 1541
    assert _row(grid, "leadgen", "revenue_booked")["mtd"] == 4271695
    assert _row(grid, "leadgen", "spends")["mtd"] == 678102


def test_overall_equals_the_sum_of_sections():
    grid = build_grid(JUNE, _base(), TODAY)
    for key in ("orders_booked", "orders_cancelled", "revenue_booked"):
        assert _row(grid, "overall", key)["mtd"] == _row(grid, "leadgen", key)["mtd"]
    assert _row(grid, "overall", "leads_total")["mtd"] == 24321


def test_weekly_buckets_are_calendar_weeks():
    """The sheet's LEADS row bucketed correctly; its Spends row did not (defect D2)."""
    grid = build_grid(JUNE, _base(), TODAY)
    # sheet agrees here -- proves our boundaries are the intended ones
    assert _row(grid, "leadgen", "leads_lsq_total")["weekly"] == [3926, 7084, 5636, 6143, 1532]
    # sheet printed [21872, 126162, 221094, 162084, 146890] -- three-day "Week 1". D2.
    assert _row(grid, "leadgen", "spends")["weekly"] == [80525, 219149, 160432, 173451, 44545]
    assert sum(_row(grid, "leadgen", "spends")["weekly"]) == 678102


def test_sunday_weekday_average_is_not_broken():
    """Defect D3: the sheet's AVERAGEIFS criteria stopped at Saturday."""
    grid = build_grid(JUNE, _base(), TODAY)
    avg = _row(grid, "leadgen", "spends")["weekday_avg"]
    assert len(avg) == 7
    assert avg[0] == (7278 + 18684 + 23758 + 25333 + 24071) / 5     # Mondays -> 19824.8
    assert avg[6] == (19449 + 46086 + 23182 + 23502) / 4            # Sundays -> 28054.75


def test_ratio_rows_aggregate_before_dividing():
    grid = build_grid(JUNE, _base(), TODAY)
    cpl = _row(grid, "leadgen", "cpl")
    assert cpl["weekly"][0] == safe_div(80525, 3926)
    assert cpl["mtd"] == safe_div(678102, 24321)


def test_cancellation_rate_formula_and_correction_flag():
    """The FORMULA (cancelled/booked) was never wrong -- 2598/4308 really is 60%.

    Defect D1 is that the sheet fed those two cells from DIFFERENT populations:
    Organic-Search orders contributed their cancellations to Lead Gen while their
    bookings sat under Organic. Feeding this test the sheet's own arrays therefore
    reproduces 60% -- correctly. The fix lives in tracker_queries.py, which derives
    both numerator and denominator from one classified population, yielding ~48%.
    This test pins the formula and asserts the row is flagged for the UI.
    """
    grid = build_grid(JUNE, _base(), TODAY)
    rate = _row(grid, "leadgen", "mtd_cancellation_rate")
    assert rate["mtd"] == safe_div(2598, 4308)      # == 0.6030... from the sheet's own arrays
    assert rate["correction"] is not None


def test_mtd_only_rows_have_no_daily_cells():
    grid = build_grid(JUNE, _base(), TODAY)
    assert _row(grid, "leadgen", "roas_booked")["daily"] is None
    assert _row(grid, "leadgen", "orders_booked")["daily"] is not None


@pytest.mark.parametrize("year,month,n_days", [
    (2026, 7, 31),   # 31-day month -- catches a hardcoded 30
    (2026, 6, 30),   # 30-day month -- catches a hardcoded 31
    (2027, 2, 28),   # 28-day month -- catches both
])
def test_estimated_revenue_uses_days_in_month(year, month, n_days):
    """Defect D4: the sheet hardcoded x30.

    One month proves nothing. Asserting 31.0 for July passes whether month_days comes
    from calendar.monthrange or from a literal 31. Three month lengths pin it down.
    """
    days = [date(year, month, d) for d in range(1, n_days + 1)]
    base = {r.id: [0] * n_days for r in ROWS if r.source != "derived"}
    base["organic.revenue_received"] = [100_000] * n_days
    grid = build_grid(days, base, date(year + 1, 1, 1))
    # 100,000/day * n_days days / 100,000 = n_days lakh
    assert grid["estimated_revenue_lakhs"] == float(n_days)


def test_empty_week_bucket_is_dash_not_zero():
    """Feb 2027 has 28 days, so 'Week 4.5 (29 - EOM)' selects no days at all.

    _sum of an empty bucket must be None (renders as an em dash), never 0 --
    a column of zeros reads as real data on a dashboard.
    """
    feb = [date(2027, 2, d) for d in range(1, 29)]
    base = {r.id: [1.0] * 28 for r in ROWS if r.source != "derived"}
    grid = build_grid(feb, base, date(2027, 3, 1))
    booked = _row(grid, "leadgen", "orders_booked")
    assert booked["weekly"][4] is None        # 29-EOM: no such days
    assert booked["weekly"][3] == 7.0         # 22-28: seven days, one each
    assert booked["mtd"] == 28.0

    labels, buckets = zip(*[(l, i) for l, i in week_buckets(feb)])
    assert buckets[4] == [], "Feb 2027 must select no days for the 29-EOM bucket"


def test_every_weekday_slot_is_populated_in_a_full_month():
    """Guards D3 from the other side: no weekday may be silently empty."""
    grid = build_grid(JUNE, _base(), TODAY)
    avg = _row(grid, "leadgen", "orders_booked")["weekday_avg"]
    assert all(v is not None for v in avg), avg
    assert len(weekday_indices(JUNE)) == 7


def test_safe_div_never_raises():
    assert safe_div(1, 0) is None
    assert safe_div(None, 5) is None
    assert safe_div(10, 4) == 2.5
