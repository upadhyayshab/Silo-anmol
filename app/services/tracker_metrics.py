"""The Business Daily Tracker metric contract.

Every row of the tracker — its label, source, format, formula and the tooltip that
explains it — is declared exactly once, here. Nothing else in the codebase may
define tracker arithmetic.

Pure module: no database, no FastAPI, no settings. It is unit-tested against the
original Google Sheet's own June numbers (tests/test_tracker_metrics.py).

Sheet defects this module deliberately does NOT reproduce (see the design doc §3):
  D1  Cancelled/Pending were counted over a wider population than Booked.
  D2  Weekly columns used shifted ranges (Spends' "Week 1" summed 3 days).
  D3  The Sunday weekday-average errored (#DIV/0!).
  D4  Estimated Revenue hardcoded x30 regardless of month length.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

WEEK_RANGES: List[Tuple[str, int, int]] = [
    ("Week 1 (1 - 7)", 1, 7),
    ("Week 2 (8 - 14)", 8, 14),
    ("Week 3 (15 - 21)", 15, 21),
    ("Week 4 (22 - 28)", 22, 28),
    ("Week 4.5 (29 - EOM)", 29, 31),
]

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def safe_div(a, b):
    """Division that yields None rather than exploding or lying.

    The sheet printed #DIV/0! and 0% interchangeably; None renders as an em dash.
    """
    if a is None or b in (None, 0):
        return None
    return float(a) / float(b)


def week_buckets(days: List[date]) -> List[Tuple[str, List[int]]]:
    """Day-of-month 1-7 / 8-14 / 15-21 / 22-28 / 29-EOM, as indices into `days`.

    Fixes sheet defect D2 — every row gets the same, correct boundaries.
    """
    out = []
    for label, lo, hi in WEEK_RANGES:
        idx = [i for i, d in enumerate(days) if lo <= d.day <= hi]
        out.append((label, idx))
    return out


def weekday_indices(days: List[date]) -> List[List[int]]:
    """Seven index lists, Monday..Sunday. Sunday is included — sheet defect D3."""
    return [[i for i, d in enumerate(days) if d.weekday() == wd] for wd in range(7)]


def _sum(values: List[Optional[float]], idx: List[int]) -> Optional[float]:
    # An empty bucket is "no such column", not zero. February has no 29-EOM week,
    # and a wall of 0s reads as real data. None renders as an em dash.
    if not idx:
        return None
    return sum(values[i] for i in idx if values[i] is not None)


def _mean(values: List[Optional[float]], idx: List[int]) -> Optional[float]:
    picked = [values[i] for i in idx if values[i] is not None]
    if not picked:
        return None
    return sum(picked) / len(picked)


@dataclass(frozen=True)
class RowSpec:
    key: str                       # unique within its section
    label: str                     # exactly as the sheet prints it
    section: str                   # overall | leadgen | d2c | organic
    source: str                    # orders | leads | input | derived
    fmt: str                       # int | inr | pct | ratio
    tooltip: str                   # shown on hover over the row label — required
    show_daily: bool = True
    show_weekly: bool = True
    show_weekday: bool = True
    formula: Optional[Callable[[Callable[[str], Optional[float]]], Optional[float]]] = None
    correction: Optional[str] = None   # non-null => the sheet was wrong here; UI flags it

    @property
    def id(self) -> str:
        return f"{self.section}.{self.key}"


SECTIONS = [
    {"key": "overall", "title": "Overall Performance", "color": "#434343"},
    {"key": "leadgen", "title": "Lead Gen", "color": "#A64D79"},
    {"key": "d2c", "title": "D2C", "color": "#70AD47"},
    {"key": "organic", "title": "Organic", "color": "#ED7D31"},
]

INPUT_KEYS = frozenset({
    "leadgen.meta_leads",
    "leadgen.spends",
    "d2c.spends",
    "d2c.sessions",
    "d2c.engaged_users",
    "d2c.add_to_cart",
})

# Rows that exist identically in every section that reports ERP orders.
_ORDER_ROWS = [
    ("orders_booked",          "int", "Count of orders whose created_at (IST) falls on this day."),
    ("units_booked",           "int", "Sum of order_items.quantity for orders booked on this day."),
    ("revenue_booked",         "inr", "Sum of (gross_amount - discount_applied) for orders booked on this day."),
    ("orders_cancelled",       "int", "Of the orders BOOKED on this day, how many are now cancelled. Settles over time."),
    ("units_cancelled",        "int", "Units on orders booked this day that are now cancelled."),
    ("units_cancelled_amount", "inr", "Value of orders booked this day that are now cancelled."),
    ("orders_pending",         "int", "Of the orders BOOKED on this day, how many are neither delivered nor cancelled yet."),
    ("units_pending",          "int", "Units on orders booked this day that are still pending."),
    ("units_pending_amount",   "inr", "Value of orders booked this day that are still pending."),
    ("orders_delivered",       "int", "Orders whose actual_delivery_date is this day — independent of when they were booked."),
    ("units_delivered",        "int", "Units delivered on this day."),
    ("revenue_received",       "inr", "Invoiced value (gross - discount) of the orders delivered on this day. Accrual, not cash: a COD order counts here on delivery, before the money is reconciled."),
]

_LEAD_ROWS = [
    ("unprocessed_leads",  "int", "Leads created this day whose stage is still 'New Lead'. A cohort outcome — it decays to zero as the leads are worked."),
    ("not_connected_rnr",  "int", "Leads created this day now at stage 'Not Reachable'."),
    ("qualified_leads",    "int", "Leads created this day now at stage 'Engaged'."),
    ("not_qualified_leads","int", "Leads created this day now at stage 'Not Qualified'."),
]


def _order_rows(section: str, booked_label: str = "Orders Booked (ERP)") -> List[RowSpec]:
    labels = {
        "orders_booked": booked_label,
        "units_booked": "Units Booked (ERP)" if section == "leadgen" else "Units Booked",
        "revenue_booked": "Revenue Booked (ERP)" if section == "leadgen" else "Revenue Booked",
        "orders_cancelled": "Orders Cancelled",
        "units_cancelled": "Unit Cancelled",
        "units_cancelled_amount": "Unit Cancelled Amount",
        "orders_pending": "Orders Pending",
        "units_pending": "Unit Pending",
        "units_pending_amount": "Unit Pending Amount",
        "orders_delivered": "Orders Delivered (ERP)",
        "units_delivered": "Units Delivered (ERP)",
        "revenue_received": "Revenue Received (ERP)",
    }
    return [
        RowSpec(key=k, label=labels[k], section=section, source="orders", fmt=f, tooltip=t)
        for k, f, t in _ORDER_ROWS
    ]


def _lead_rows(section: str) -> List[RowSpec]:
    labels = {
        "unprocessed_leads": "Unprocessed Leads (Fresh Leads LSQ)",
        "not_connected_rnr": "Not Connected Leads (RNR) (LSQ)" if section == "leadgen" else "Not Connected Leads",
        "qualified_leads": "Qualified Leads (Engaged on LSQ) (Pending)" if section == "leadgen" else "Qualified Leads",
        "not_qualified_leads": "Not Qualified Leads (LSQ)",
    }
    return [
        RowSpec(key=k, label=labels[k], section=section, source="leads", fmt=f, tooltip=t)
        for k, f, t in _LEAD_ROWS
    ]


_ADDITIVE = [
    ("leads_total",            "Leads (Total)",                       "int"),
    ("unprocessed_leads",      "Unprocessed Leads (Fresh Leads LSQ)", "int"),
    ("not_connected_rnr",      "Not Connected Leads (RNR) (LSQ)",     "int"),
    ("qualified_leads",        "Qualified Leads (Engaged on LSQ) (Pending)", "int"),
    ("not_qualified_leads",    "Not Qualified Leads (LSQ)",           "int"),
    ("orders_booked",          "Orders Booked (ERP)",                 "int"),
    ("units_booked",           "Units Booked (ERP)",                  "int"),
    ("revenue_booked",         "Revenue Booked (ERP)",                "inr"),
    ("orders_cancelled",       "Orders Cancelled",                    "int"),
    ("units_cancelled",        "Unit Cancelled",                      "int"),
    ("units_cancelled_amount", "Unit Cancelled Amount",               "inr"),
    ("orders_pending",         "Orders Pending",                      "int"),
    ("units_pending",          "Unit Pending",                        "int"),
    ("units_pending_amount",   "Unit Pending Amount",                 "inr"),
    ("orders_delivered",       "Orders Delivered (ERP)",              "int"),
    ("units_delivered",        "Units Delivered (ERP)",               "int"),
    ("revenue_received",       "Revenue Received (ERP)",              "inr"),
]


def _additive(key: str):
    """Overall = Lead Gen + D2C + Organic. The Lead Gen leads row is named differently."""
    lg = "leadgen.leads_lsq_total" if key == "leads_total" else f"leadgen.{key}"
    def f(g, _lg=lg, _k=key):
        parts = [g(_lg), g(f"d2c.{_k}"), g(f"organic.{_k}")]
        vals = [p for p in parts if p is not None]
        return sum(vals) if vals else None
    return f


_OVERALL = [
    RowSpec(key=k, label=lbl, section="overall", source="derived", fmt=fmt,
            formula=_additive(k),
            tooltip="Lead Gen + D2C + Organic, summed for this day.")
    for k, lbl, fmt in _ADDITIVE
] + [
    RowSpec(key="mtd_o2d", label="MTD O2D (Orders)", section="overall", source="derived", fmt="pct",
            show_daily=False, show_weekly=False, show_weekday=False,
            formula=lambda g: safe_div(g("overall.orders_delivered"),
                                       (g("overall.orders_booked") or 0) - (g("overall.orders_cancelled") or 0)),
            tooltip="orders_delivered / (orders_booked - orders_cancelled). Delivered is dated by delivery; booked and cancelled by the booking day."),
]

_LEADGEN = [
    RowSpec("meta_leads", "Meta Leads", "leadgen", "input", "int",
            "Hand-entered from Meta Ads Manager. Meta's own lead count, before de-duplication."),
    RowSpec("leads_lsq_total", "Leads LSQ (TOTAL)", "leadgen", "leads", "int",
            "Leads created this day whose source is a paid or direct-response channel."),
    RowSpec("duplication_diff_pct", "Duplication Difference %", "leadgen", "derived", "pct",
            "(meta_leads - leads_lsq_total) / meta_leads. How many of Meta's reported leads were duplicates.",
            formula=lambda g: safe_div((g("leadgen.meta_leads") or 0) - (g("leadgen.leads_lsq_total") or 0),
                                       g("leadgen.meta_leads"))),
    *_lead_rows("leadgen")[:1],                       # unprocessed_leads
    *_lead_rows("leadgen")[1:2],                      # not_connected_rnr
    RowSpec("not_connected_pct", "Not Connected %", "leadgen", "derived", "pct",
            "not_connected_rnr / leads_lsq_total for this day.",
            formula=lambda g: safe_div(g("leadgen.not_connected_rnr"), g("leadgen.leads_lsq_total"))),
    *_lead_rows("leadgen")[2:],                       # qualified, not_qualified
    *_order_rows("leadgen"),
]

_MTD_ONLY = dict(show_daily=False, show_weekly=False, show_weekday=False)

_LEADGEN += [
    RowSpec("mtd_ql_pct", "MTD QL %", "leadgen", "derived", "pct", **_MTD_ONLY,
            tooltip="(qualified_leads + orders_booked) / leads_lsq_total. Orders booked stand in for leads that converted.",
            formula=lambda g: safe_div((g("leadgen.qualified_leads") or 0) + (g("leadgen.orders_booked") or 0),
                                       g("leadgen.leads_lsq_total"))),
    RowSpec("mtd_not_qualified_pct", "MTD Not Qualified %", "leadgen", "derived", "pct", **_MTD_ONLY,
            tooltip="not_qualified_leads / leads_lsq_total.",
            formula=lambda g: safe_div(g("leadgen.not_qualified_leads"), g("leadgen.leads_lsq_total"))),
    RowSpec("mtd_not_connected_pct", "MTD Not Connected %", "leadgen", "derived", "pct", **_MTD_ONLY,
            tooltip="not_connected_rnr / leads_lsq_total.",
            formula=lambda g: safe_div(g("leadgen.not_connected_rnr"), g("leadgen.leads_lsq_total"))),
    RowSpec("mtd_unprocessed_pct", "MTD Unprocessed %", "leadgen", "derived", "pct", **_MTD_ONLY,
            tooltip="unprocessed_leads / leads_lsq_total.",
            formula=lambda g: safe_div(g("leadgen.unprocessed_leads"), g("leadgen.leads_lsq_total"))),
    RowSpec("mtd_conversion_pct", "MTD Conversion Rate %", "leadgen", "derived", "pct", **_MTD_ONLY,
            tooltip="orders_booked / (qualified_leads + orders_booked).",
            formula=lambda g: safe_div(g("leadgen.orders_booked"),
                                       (g("leadgen.qualified_leads") or 0) + (g("leadgen.orders_booked") or 0))),
    RowSpec("mtd_cancellation_rate", "MTD Cancellation Rate (Orders)", "leadgen", "derived", "pct", **_MTD_ONLY,
            tooltip="orders_cancelled / orders_booked, both over the SAME population of orders.",
            correction="The sheet divided cancellations counted across a wider population by a narrower booked count, "
                       "reporting 60%. Measured consistently the rate is about 48%.",
            formula=lambda g: safe_div(g("leadgen.orders_cancelled"), g("leadgen.orders_booked"))),
    RowSpec("mtd_o2d", "MTD O2D (Orders)", "leadgen", "derived", "pct", **_MTD_ONLY,
            tooltip="orders_delivered / (orders_booked - orders_cancelled).",
            formula=lambda g: safe_div(g("leadgen.orders_delivered"),
                                       (g("leadgen.orders_booked") or 0) - (g("leadgen.orders_cancelled") or 0))),
    RowSpec("spends", "Spends", "leadgen", "input", "inr",
            "Hand-entered from Meta Ads Manager. Lead-gen ad spend for this day."),
    RowSpec("cpl", "CPL", "leadgen", "derived", "inr",
            "spends / leads_lsq_total. Weekly and weekday figures divide the aggregated spend by the aggregated leads — not an average of daily ratios.",
            formula=lambda g: safe_div(g("leadgen.spends"), g("leadgen.leads_lsq_total"))),
    RowSpec("cpql", "CPQL", "leadgen", "derived", "inr", **_MTD_ONLY,
            tooltip="spends / qualified_leads.",
            formula=lambda g: safe_div(g("leadgen.spends"), g("leadgen.qualified_leads"))),
    RowSpec("auv_booked", "AUV Booked", "leadgen", "derived", "inr",
            show_daily=False, show_weekly=True, show_weekday=True,
            tooltip="revenue_booked / units_booked. Average unit value.",
            formula=lambda g: safe_div(g("leadgen.revenue_booked"), g("leadgen.units_booked"))),
    RowSpec("cpt_booked", "CPT Booked", "leadgen", "derived", "inr",
            show_daily=False, show_weekly=True, show_weekday=True,
            tooltip="spends / units_booked. Note: the D2C section's CPT divides by ORDERS, not units — the sheet used one label for two definitions.",
            correction="Same label, two definitions across sections. Lead Gen divides by units; D2C divides by orders.",
            formula=lambda g: safe_div(g("leadgen.spends"), g("leadgen.units_booked"))),
    RowSpec("roas_booked", "RoAS Booked", "leadgen", "derived", "ratio", **_MTD_ONLY,
            tooltip="revenue_booked / spends.",
            formula=lambda g: safe_div(g("leadgen.revenue_booked"), g("leadgen.spends"))),
    RowSpec("units_per_order", "Units/Order (Booked)", "leadgen", "derived", "ratio", **_MTD_ONLY,
            tooltip="units_booked / orders_booked.",
            formula=lambda g: safe_div(g("leadgen.units_booked"), g("leadgen.orders_booked"))),
]

_D2C_PREPAID = RowSpec("prepaid_booked_orders", "Pre-paid Booked Orders", "d2c", "orders", "int",
                       "Orders booked this day with prepaid_amount > 0. Computed from the ERP, not hand-entered.")

# The sheet prints Pre-paid between Units Booked and Revenue Booked. _order_rows emits
# [orders_booked, units_booked, revenue_booked, ...], so splice at index 2 -- do NOT append.
_D2C_ORDERS = _order_rows("d2c", booked_label="Order Booked")
_D2C_ORDERS = _D2C_ORDERS[:2] + [_D2C_PREPAID] + _D2C_ORDERS[2:]

_D2C = [
    RowSpec("leads_total", "Leads (Total)", "d2c", "leads", "int",
            "Leads created this day whose source is the Medusa storefront."),
    *_lead_rows("d2c"),
    RowSpec("sessions", "Sessions", "d2c", "input", "int", "Hand-entered from GA4."),
    RowSpec("engaged_users", "Engaged Users (GA4)", "d2c", "input", "int", "Hand-entered from GA4."),
    RowSpec("add_to_cart", "Add to Cart", "d2c", "input", "int", "Hand-entered from GA4."),
    *_D2C_ORDERS,
    RowSpec("mtd_conversion_rate", "MTD Conversion Rate", "d2c", "derived", "pct", **_MTD_ONLY,
            tooltip="orders_booked / engaged_users (GA4). Retained as the business defines it.",
            correction="Divides orders by GA4 engaged users rather than by sessions or leads.",
            formula=lambda g: safe_div(g("d2c.orders_booked"), g("d2c.engaged_users"))),
    RowSpec("sessions_to_cart", "SessionstoCart", "d2c", "derived", "pct", **_MTD_ONLY,
            tooltip="add_to_cart / sessions.",
            formula=lambda g: safe_div(g("d2c.add_to_cart"), g("d2c.sessions"))),
    RowSpec("cart2order", "Cart2Order", "d2c", "derived", "pct", **_MTD_ONLY,
            tooltip="orders_booked / add_to_cart.",
            formula=lambda g: safe_div(g("d2c.orders_booked"), g("d2c.add_to_cart"))),
    RowSpec("share_of_prepaid", "Share of Pre-paid", "d2c", "derived", "pct", **_MTD_ONLY,
            tooltip="prepaid_booked_orders / orders_booked.",
            formula=lambda g: safe_div(g("d2c.prepaid_booked_orders"), g("d2c.orders_booked"))),
    RowSpec("mtd_o2d", "MTD O2D", "d2c", "derived", "pct", **_MTD_ONLY,
            tooltip="units_delivered / orders_booked, as the business defines it.",
            correction="Divides UNITS delivered by ORDERS booked. Every other O2D on this page is orders over orders.",
            formula=lambda g: safe_div(g("d2c.units_delivered"), g("d2c.orders_booked"))),
    RowSpec("spends", "Spends", "d2c", "input", "inr", "Hand-entered from Meta Ads Manager. D2C ad spend for this day."),
    RowSpec("cp_atc", "CP-ATC", "d2c", "derived", "inr",
            show_daily=False, show_weekly=True, show_weekday=False,
            tooltip="spends / add_to_cart.",
            formula=lambda g: safe_div(g("d2c.spends"), g("d2c.add_to_cart"))),
    RowSpec("auv_booked", "AUV Booked", "d2c", "derived", "inr",
            show_daily=False, show_weekly=True, show_weekday=False,
            tooltip="revenue_booked / units_booked.",
            correction="The sheet's weekly AUV divided revenue by ORDERS while its MTD figure divided by UNITS. Both now use units.",
            formula=lambda g: safe_div(g("d2c.revenue_booked"), g("d2c.units_booked"))),
    RowSpec("cpt_booked", "CPT Booked", "d2c", "derived", "inr",
            show_daily=False, show_weekly=True, show_weekday=False,
            tooltip="spends / orders_booked. Lead Gen's CPT divides by units instead.",
            correction="Same label, two definitions across sections.",
            formula=lambda g: safe_div(g("d2c.spends"), g("d2c.orders_booked"))),
    RowSpec("cost_per_session", "Cost per Session", "d2c", "derived", "inr", **_MTD_ONLY,
            tooltip="spends / sessions.",
            formula=lambda g: safe_div(g("d2c.spends"), g("d2c.sessions"))),
    RowSpec("cpql", "CPQL", "d2c", "derived", "inr", **_MTD_ONLY,
            tooltip="spends / engaged_users. Named CPQL but the denominator is GA4 engaged users, not qualified leads.",
            correction="Denominator is GA4 engaged users, not qualified leads.",
            formula=lambda g: safe_div(g("d2c.spends"), g("d2c.engaged_users"))),
    RowSpec("roas_booked", "RoAS (Booked)", "d2c", "derived", "ratio", **_MTD_ONLY,
            tooltip="revenue_booked / spends.",
            formula=lambda g: safe_div(g("d2c.revenue_booked"), g("d2c.spends"))),
    RowSpec("units_per_order", "Units/Order (Booked)", "d2c", "derived", "ratio", **_MTD_ONLY,
            tooltip="units_booked / orders_booked.",
            formula=lambda g: safe_div(g("d2c.units_booked"), g("d2c.orders_booked"))),
]

_ORGANIC = [
    RowSpec("leads_total", "Leads (Total)", "organic", "leads", "int",
            "Leads created this day from organic search, referral, direct or unattributed sources."),
    *_lead_rows("organic"),
    *_order_rows("organic", booked_label="Order Booked"),
]

ROWS: List[RowSpec] = _OVERALL + _LEADGEN + _D2C + _ORGANIC
ROWS_BY_ID = {r.id: r for r in ROWS}


def _evaluate(column_values: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """Fill in every derived row for one column, in declaration order.

    Declaration order is dependency-safe: the Overall additive rows read only base
    rows, and overall.mtd_o2d is declared after them.
    """
    def g(row_id: str) -> Optional[float]:
        return column_values.get(row_id)

    for row in ROWS:
        if row.source == "derived" and row.formula is not None:
            column_values[row.id] = row.formula(g)
    return column_values


def build_grid(days: List[date], base: Dict[str, List[Optional[float]]], today: date) -> dict:
    """`base` maps row id -> one value per day, for every orders/leads/input row."""
    n = len(days)
    weeks = week_buckets(days)
    wd_idx = weekday_indices(days)

    def zeros() -> List[Optional[float]]:
        return [None] * n

    # ---- daily columns -------------------------------------------------------
    daily: List[Dict[str, Optional[float]]] = []
    for i in range(n):
        col = {r.id: (base.get(r.id, zeros())[i] if r.source != "derived" else None) for r in ROWS}
        daily.append(_evaluate(col))

    # ---- MTD, weekly, weekday: aggregate BASE rows, then re-derive ------------
    def aggregate(idx: List[int], how: str) -> Dict[str, Optional[float]]:
        col: Dict[str, Optional[float]] = {}
        for r in ROWS:
            if r.source == "derived":
                col[r.id] = None
                continue
            series = base.get(r.id, zeros())
            col[r.id] = _sum(series, idx) if how == "sum" else _mean(series, idx)
        return _evaluate(col)

    all_idx = list(range(n))
    mtd = aggregate(all_idx, "sum")
    weekly = [aggregate(idx, "sum") for _, idx in weeks]
    weekday = [aggregate(idx, "mean") for idx in wd_idx]

    # ---- estimated revenue (fixes D4: real days in month, never 30) -----------
    month_days = calendar.monthrange(days[0].year, days[0].month)[1]
    elapsed = min(today, days[-1]).day if today >= days[0] else 0
    est = safe_div(mtd["overall.revenue_received"], elapsed)
    estimated_revenue_lakhs = (est * month_days / 100_000) if est else None

    # ---- payload -------------------------------------------------------------
    sections = []
    for s in SECTIONS:
        rows = []
        for r in ROWS:
            if r.section != s["key"]:
                continue
            rows.append({
                "key": r.key,
                "id": r.id,
                "label": r.label,
                "fmt": r.fmt,
                "source": r.source,
                "editable": r.id in INPUT_KEYS,
                "tooltip": r.tooltip,
                "correction": r.correction,
                "daily": [daily[i][r.id] for i in range(n)] if r.show_daily else None,
                "mtd": mtd[r.id],
                "weekly": [w[r.id] for w in weekly] if r.show_weekly else None,
                "weekday_avg": [w[r.id] for w in weekday] if r.show_weekday else None,
            })
        sections.append({**s, "rows": rows})

    return {
        "days": [d.isoformat() for d in days],
        "weekday_labels": [WEEKDAY_NAMES[d.weekday()] for d in days],
        "weeks": [label for label, _ in weeks],
        "weekdays": WEEKDAY_NAMES,
        "estimated_revenue_lakhs": estimated_revenue_lakhs,
        "sections": sections,
    }
