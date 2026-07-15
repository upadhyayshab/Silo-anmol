"""State x stage pivot ("Prospect Pivot" dashboard) — the pure assembly against the
Google-Sheet the business gave us (1-Jul'26 to 5-Jul'26). Reproduces every published
cell so the money logic (Attempted / Connected / Conv / Not-Connected %, Avg Lead/Day)
can't silently drift. DB-free — feeds build_state_pivot a hand-built count map. Run::

    python tests/test_state_pivot.py
    # or: pytest tests/test_state_pivot.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports

from services.crmReportService import build_state_pivot  # noqa: E402


# The screenshot's stage counts, per state. (state, stage) -> count; zeros omitted.
SHEET = {
    "Unknown":          {"Engaged": 16, "FTU": 17,  "Not Qualified": 11,  "Not Reachable": 14,   "RTU": 3},
    "Andhra Pradesh": {"Engaged": 264, "FTU": 144, "Not Qualified": 363, "Not Reachable": 723,  "RTU": 3},
    "Karnataka":      {"Engaged": 1047, "FTU": 346, "New Lead": 5, "Not Qualified": 206, "Not Reachable": 1009, "RTU": 15},
    "Punjab":         {"Engaged": 38, "FTU": 19,  "New Lead": 1, "Not Qualified": 81,  "Not Reachable": 162},
    "Telangana":      {"FTU": 10},
}


def _counts():
    return {(state, stage): n for state, stages in SHEET.items() for stage, n in stages.items()}


def _row(pivot, label):
    return next(r for r in pivot["rows"] if r["label"] == label)


def test_columns_blank_first_then_alpha_then_grand_total():
    p = build_state_pivot(_counts(), date(2026, 7, 1), date(2026, 7, 5))
    assert p["columns"] == ["Unknown", "Andhra Pradesh", "Karnataka", "Punjab", "Telangana", "Grand Total"]


def test_grand_totals_match_sheet():
    p = build_state_pivot(_counts(), date(2026, 7, 1), date(2026, 7, 5))
    gt = _row(p, "Grand Total")["values"]
    assert gt == {"Unknown": 61, "Andhra Pradesh": 1497, "Karnataka": 2628,
                  "Punjab": 301, "Telangana": 10, "Grand Total": 4497}


def test_percentages_match_sheet():
    p = build_state_pivot(_counts(), date(2026, 7, 1), date(2026, 7, 5))
    att = _row(p, "Attempted %")["values"]
    con = _row(p, "Lead to Connected %")["values"]
    conv = _row(p, "Lead to Conv%")["values"]
    ncon = _row(p, "Not Connected")["values"]
    # Grand Total column
    assert att["Grand Total"] == 99.9
    assert con["Grand Total"] == 57.4
    assert conv["Grand Total"] == 12.4
    assert ncon["Grand Total"] == 42.4
    # A couple of state columns end-to-end
    assert (att["Karnataka"], con["Karnataka"], conv["Karnataka"], ncon["Karnataka"]) == (99.8, 61.4, 13.7, 38.4)
    assert (att["Unknown"], con["Unknown"], conv["Unknown"], ncon["Unknown"]) == (100.0, 77.0, 32.8, 23.0)
    assert ncon["Telangana"] == 0.0  # no Not Reachable
    # Not Qualified % = Not Qualified / Grand Total (red warn row, like Not Connected)
    nq = _row(p, "Not Qualified %")["values"]
    assert nq["Grand Total"] == 14.7   # 661 / 4497
    assert nq["Karnataka"] == 7.8      # 206 / 2628
    assert nq["Telangana"] == 0.0      # no Not Qualified


def test_avg_lead_per_day_divides_by_window_days():
    # 1-Jul..5-Jul inclusive = 5 days.
    p = build_state_pivot(_counts(), date(2026, 7, 1), date(2026, 7, 5))
    avg = _row(p, "Avg Lead/Day")["values"]
    assert avg == {"Unknown": 12, "Andhra Pradesh": 299, "Karnataka": 526,
                   "Punjab": 60, "Telangana": 2, "Grand Total": 899}


def test_no_window_avg_is_total_not_divide_by_zero():
    p = build_state_pivot(_counts())  # no dates -> days defaults to 1
    assert p["days"] == 1
    assert _row(p, "Avg Lead/Day")["values"]["Grand Total"] == 4497


def test_empty_counts_is_safe():
    p = build_state_pivot({}, date(2026, 7, 1), date(2026, 7, 5))
    assert p["columns"] == ["Grand Total"]
    assert _row(p, "Grand Total")["values"] == {"Grand Total": 0}
    assert _row(p, "Attempted %")["values"]["Grand Total"] == 0.0  # no divide-by-zero


# --- Task F: order/qty/booked-rev rows (order_by_state) ---------------------
# The 4 new rows are order-centric (customer_orders.state), mirroring the Daily-Rev
# `placed` CTE's net_amount = gross - discount. Grand Total = sum across state
# columns is the hard tie-out invariant (must equal Daily-Rev "Placed").

_ORDER_BY_STATE = {
    "Karnataka": {"orders": 3, "booked": 4050.0, "qty": 10},
    "Punjab": {"orders": 2, "booked": 1400.0, "qty": 10},
}


def test_order_rows_appear_after_avg_lead_day_with_correct_values():
    p = build_state_pivot(_counts(), date(2026, 7, 1), date(2026, 7, 5), order_by_state=_ORDER_BY_STATE)
    labels = [r["label"] for r in p["rows"]]
    i = labels.index("Avg Lead/Day")
    assert labels[i + 1:i + 5] == ["No. of Orders", "Total Qty", "Booked Revenue", "Avg Booked Rev/Day"]

    assert _row(p, "No. of Orders")["values"] == {"Karnataka": 3, "Punjab": 2, "Grand Total": 5}
    assert _row(p, "Total Qty")["values"] == {"Karnataka": 10, "Punjab": 10, "Grand Total": 20}
    assert _row(p, "Booked Revenue")["values"] == {"Karnataka": 4050.0, "Punjab": 1400.0, "Grand Total": 5450.0}
    # 5-day window: booked / days, rounded to whole rupee.
    assert _row(p, "Avg Booked Rev/Day")["values"] == {"Karnataka": 810, "Punjab": 280, "Grand Total": 1090}

    assert _row(p, "No. of Orders")["type"] == "num"
    assert _row(p, "Total Qty")["type"] == "num"
    assert _row(p, "Booked Revenue")["type"] == "currency"
    assert _row(p, "Avg Booked Rev/Day")["type"] == "currency"


def test_order_rows_grand_total_ties_to_sum_of_state_columns():
    """The hard acceptance criterion: Grand Total column = sum of the (order-only-aware)
    state columns, for every one of the 4 rows — this is what makes the Grand Total
    match Daily-Rev's "Placed" figures."""
    order_by_state = {
        "Karnataka": {"orders": 3, "booked": 4050.0, "qty": 10},
        "Punjab": {"orders": 2, "booked": 1400.0, "qty": 10},
        "Telangana": {"orders": 1, "booked": 500.0, "qty": 2},
    }
    p = build_state_pivot(_counts(), date(2026, 7, 1), date(2026, 7, 5), order_by_state=order_by_state)
    state_cols = [c for c in p["columns"] if c != "Grand Total"]
    for label in ("No. of Orders", "Total Qty", "Booked Revenue"):
        values = _row(p, label)["values"]
        assert values["Grand Total"] == sum(values.get(c, 0) for c in state_cols)


def test_order_only_state_gets_a_column_not_present_in_lead_counts():
    # Gujarat has orders but no leads in the window -> still gets a column so its
    # order figures aren't silently dropped from the Grand Total.
    order_by_state = {"Gujarat": {"orders": 1, "booked": 100.0, "qty": 1}}
    p = build_state_pivot(_counts(), date(2026, 7, 1), date(2026, 7, 5), order_by_state=order_by_state)
    assert "Gujarat" in p["columns"]
    assert _row(p, "No. of Orders")["values"]["Gujarat"] == 1
    assert _row(p, "No. of Orders")["values"]["Grand Total"] == 1
    # Gujarat has no leads, so its stage/Grand-Total (lead) row stays 0/absent.
    assert "Gujarat" not in _row(p, "Grand Total")["values"] or _row(p, "Grand Total")["values"]["Gujarat"] == 0


def test_zero_or_absent_order_value_is_blank_like_stage_rows():
    order_by_state = {"Karnataka": {"orders": 0, "booked": 0.0, "qty": 0}}
    p = build_state_pivot(_counts(), date(2026, 7, 1), date(2026, 7, 5), order_by_state=order_by_state)
    assert "Karnataka" not in _row(p, "No. of Orders")["values"]
    assert "Karnataka" not in _row(p, "Booked Revenue")["values"]


def test_no_order_by_state_means_no_order_rows_unaffected_columns():
    # This is exactly call_direction_pivot's call shape: build_state_pivot(counts, ...)
    # with no order_by_state at all.
    p = build_state_pivot(_counts(), date(2026, 7, 1), date(2026, 7, 5))
    labels = {r["label"] for r in p["rows"]}
    assert not ({"No. of Orders", "Total Qty", "Booked Revenue", "Avg Booked Rev/Day"} & labels)
    assert p["columns"] == ["Unknown", "Andhra Pradesh", "Karnataka", "Punjab", "Telangana", "Grand Total"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all pass")
