"""CRM Superadmin "Prospect Report" — one flat row per prospect joining the
lead, its latest call disposition, its call-attempt count, and its order rollup.

Mirrors the LeadSquared-style export the business is used to. The Excel export is
built client-side (xlsx) like the rest of the app's reports; this layer just
returns the rows (pass limit=0 for the full filtered set).

ponytail: the page/export is assembled with a few batched queries over the
matched lead ids and rolled up in Python. Fine at page scale and for the bounded
export below; if exports ever exceed MAX_EXPORT_ROWS, switch to a streamed,
keyset-paginated cursor instead of one in-memory build.
"""
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple

import sqlalchemy as db

from managers import (
    LeadManager, LeadSchema, LeadActivitySchema,
    CustomerOrderSchema, OrderItemSchema, UserSchema, OutletSchema, AgencySchema,
)
from utils.crm_enums import LeadActivityType, CallOutcome, LeadStage
from utils.crm_constants import LeadSource
from utils.timeutils import IST


def _enum_or_raw(enum_cls, val):
    """Coerce a query string to its enum member (canonical compare); fall back to
    the raw string so legacy/partial values still match instead of erroring."""
    if not val:
        return None
    try:
        return enum_cls(val)
    except ValueError:
        return val

# Hard ceiling so a "download everything" never OOMs the single-task prod box.
MAX_EXPORT_ROWS = 100_000

# Latest call outcome -> the report's Call Attempt bucket (matches the sheet:
# Answered / Call failure / Not Answered / Not Attempted).
_CALL_ATTEMPT = {
    CallOutcome.ANSWERED.value: "Answered",
    CallOutcome.CALL_BACK_LATER.value: "Answered",   # connected & spoke
    CallOutcome.NOT_ANSWERED.value: "Not Answered",
    CallOutcome.BUSY.value: "Call failure",
    CallOutcome.SWITCHED_OFF.value: "Call failure",
    CallOutcome.WRONG_NUMBER.value: "Call failure",
}


def _call_attempt_label(outcome: Optional[str]) -> str:
    if outcome is None:
        return "Not Attempted"
    return _CALL_ATTEMPT.get(outcome, "Not Answered")


# Outcomes that count as a live conversation -> "Connected".
_CONNECTED_OUTCOMES = {CallOutcome.ANSWERED.value, CallOutcome.CALL_BACK_LATER.value}


def _disposition_label(details: dict, outcome: Optional[str]) -> str:
    """Top-level disposition for the report. Prefer the telecaller's explicit
    two-level pick; otherwise derive Connected/Not Connected from the outcome —
    inbound auto-logs and simple-outcome calls never went through the picker, so
    `details` has no disposition even though they were clearly answered/missed."""
    picked = (details or {}).get("disposition")
    if picked:
        return picked
    if outcome is None:
        return ""
    return "Connected" if outcome in _CONNECTED_OUTCOMES else "Not Connected"


# The business runs on IST; created_at/order_date are stored UTC timestamptz. Build
# day bounds in IST so a picked calendar day means that day in IST, not UTC (else a
# "to Jul 4" filter leaks in leads created early Jul 5 IST = late Jul 4 UTC).
#
# NOTE: unlike utils.timeutils.ist_day_bounds, this does NOT default to "today"
# when both args are None — an optional report filter with no range means "no
# filter", not "today only". Keep this local (don't swap to the shared helper).
def _day_bounds(from_date: Optional[date], to_date: Optional[date]):
    """Inclusive IST calendar-day bounds, tz-aware; the DB compares instants so the
    IST offset resolves to the correct UTC window against the UTC-stored columns."""
    gte = datetime.combine(from_date, datetime.min.time(), tzinfo=IST) if from_date else None
    lte = datetime.combine(to_date, datetime.max.time(), tzinfo=IST) if to_date else None
    return gte, lte


def _num(value) -> float:
    return float(value) if isinstance(value, Decimal) else (value or 0)


def _order_date_conds(order_from_date, order_to_date) -> list:
    """Conds restricting `customer_orders.order_date` to the inclusive IST day window."""
    o_gte, o_lte = _day_bounds(order_from_date, order_to_date)
    conds = []
    if o_gte is not None:
        conds.append(CustomerOrderSchema.order_date >= o_gte)
    if o_lte is not None:
        conds.append(CustomerOrderSchema.order_date <= o_lte)
    return conds


def order_window_clause(order_from_date=None, order_to_date=None):
    """EXISTS clause: the lead has >=1 order inside the IST day window; None when no
    window is given (caller applies no filter). Shared by the reports and the Manage
    Leads list so both interpret an order-date range identically."""
    conds = _order_date_conds(order_from_date, order_to_date)
    if not conds:
        return None
    return db.exists().where(CustomerOrderSchema.lead_id == LeadSchema.uid, *conds)


def _owner_scope_conds(scope_owner_id=None, agency_id: Optional[str] = None) -> list:
    """Owner-scope conds shared by every report: `agency_id` restricts to leads whose
    owner belongs to that agency (owner_id -> users.agency_id, same correlated-subquery
    shape as the Manage Leads list); `scope_owner_id` restricts to one owner or a list of
    owners (agency admins' roster). Conds are against `LeadSchema.owner_id` — a caller
    scoping a DIFFERENT table (e.g. customer_orders, see `_order_state_aggregate`) must
    join `LeadSchema` in first. Factored out of `_lead_filter_conds` so both reuse the
    identical scope logic instead of it drifting between two hand-copies."""
    conds = []
    if agency_id:
        conds.append(LeadSchema.owner_id.in_(
            db.select(UserSchema.uid).where(UserSchema.agency_id == agency_id)))
    if scope_owner_id is not None:
        conds.append(LeadSchema.owner_id.in_(scope_owner_id)
                     if isinstance(scope_owner_id, (list, tuple, set))
                     else LeadSchema.owner_id == scope_owner_id)
    return conds


def _lead_filter_conds(*, from_date=None, to_date=None, order_from_date=None,
                       order_to_date=None, region=None, regions=None, owner_id=None,
                       owner_ids=None, stage=None, source=None, lead_numbers=None,
                       scope_owner_id=None, agency_id=None, extra_clause=None, gate_order_date=True):
    """Shared WHERE conds for the lead-based reports (prospect + agent performance).

    Returns ``(conds, order_date_conds)``. `region`/`regions` fold through canon_state
    (so Telangana lands in the AP pool) and `regions` (a list) drives a multi-select
    ``state IN (...)``. When `gate_order_date` is True an order-date window also gates
    leads to those with an in-window order (prospect report); when False the window
    only scopes the caller's own order rollup and never drops leads (agent report)."""
    from services.leadService import canon_state
    conds = [LeadSchema.deleted_at.is_(None)]
    gte, lte = _day_bounds(from_date, to_date)
    if gte is not None:
        conds.append(LeadSchema.created_at >= gte)
    if lte is not None:
        conds.append(LeadSchema.created_at <= lte)

    order_date_conds = _order_date_conds(order_from_date, order_to_date)
    if order_date_conds and gate_order_date:
        conds.append(order_window_clause(order_from_date, order_to_date))

    # lead.state is stored lowercase-canonical; canonicalize the incoming filter(s) so any
    # casing matches and Telangana folds to the AP team. `regions` (multi-select) wins over
    # the legacy single `region`.
    region_list = regions if regions else ([region] if region else None)
    if region_list:
        canon = [c for c in (canon_state(r) for r in region_list) if c]
        if canon:
            conds.append(LeadSchema.state.in_(canon))
    if owner_ids:
        conds.append(LeadSchema.owner_id.in_(owner_ids))
    elif owner_id:
        conds.append(LeadSchema.owner_id == owner_id)
    if stage:
        conds.append(LeadSchema.stage == _enum_or_raw(LeadStage, stage))
    if source:
        conds.append(LeadSchema.source == _enum_or_raw(LeadSource, source))
    if lead_numbers:
        conds.append(LeadSchema.lead_number.in_(lead_numbers))
    # Agency filter: leads whose owner belongs to this agency (owner_id -> users.agency_id).
    # Same correlated-subquery shape as the Manage Leads list, so both agree on membership.
    conds.extend(_owner_scope_conds(scope_owner_id, agency_id))
    if extra_clause is not None:
        conds.append(extra_clause)
    return conds, order_date_conds


async def prospect_report(
    engine, *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    order_from_date: Optional[date] = None,
    order_to_date: Optional[date] = None,
    region: Optional[str] = None,
    regions: Optional[List[str]] = None,
    owner_id: Optional[str] = None,
    owner_ids: Optional[List[str]] = None,
    stage: Optional[str] = None,
    source: Optional[str] = None,
    lead_numbers: Optional[List[str]] = None,
    scope_owner_id: Optional[str] = None,
    agency_id: Optional[str] = None,
    extra_clause=None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Return (rows, total). `limit=0` means "all matched rows" (capped at
    MAX_EXPORT_ROWS) — used by the CSV export. `scope_owner_id` restricts to one
    owner's leads, or a list of owner ids (agency admins — their agency roster);
    None = all leads (superadmin).
    `extra_clause` is an optional pre-built SQLAlchemy boolean clause (from the
    advanced filter engine) AND-ed into the same `conds` used by both queries."""
    lm = LeadManager(engine)
    async with lm.session_factory() as session:
        # Order-date window gates prospects to those with an in-window order AND scopes the
        # rollup below to those same orders, so counts/amounts reflect the period.
        conds, order_date_conds = _lead_filter_conds(
            from_date=from_date, to_date=to_date,
            order_from_date=order_from_date, order_to_date=order_to_date,
            region=region, regions=regions, owner_id=owner_id, owner_ids=owner_ids,
            stage=stage, source=source, lead_numbers=lead_numbers,
            scope_owner_id=scope_owner_id, agency_id=agency_id,
            extra_clause=extra_clause, gate_order_date=True)

        total = int((await session.execute(
            db.select(db.func.count()).select_from(LeadSchema).where(*conds)
        )).scalar_one())

        lead_q = (db.select(LeadSchema).where(*conds)
                  .order_by(LeadSchema.created_at.desc()).offset(offset))
        lead_q = lead_q.limit(limit if limit else MAX_EXPORT_ROWS)
        leads = list((await session.execute(lead_q)).scalars().all())
        if not leads:
            return [], total

        lead_ids = [l.uid for l in leads]
        owner_ids = {l.owner_id for l in leads if l.owner_id}

        # --- owner names + email (owner_email is the telecaller's; the lead's own
        # contact details — name, phone — come from the lead row itself below) ---
        owners: Dict[str, str] = {}
        owner_emails: Dict[str, str] = {}
        if owner_ids:
            for uid, name, email in (await session.execute(
                db.select(UserSchema.uid, UserSchema.full_name, UserSchema.email)
                  .where(UserSchema.uid.in_(owner_ids))
            )).all():
                owners[uid] = name or email or uid
                owner_emails[uid] = email or ""

        # --- call activities: count + latest disposition per lead ---
        call_count: Dict[str, int] = {}
        latest_call: Dict[str, LeadActivitySchema] = {}
        acts = (await session.execute(
            db.select(LeadActivitySchema)
              .where(LeadActivitySchema.lead_id.in_(lead_ids),
                     LeadActivitySchema.activity_type == LeadActivityType.CALL_LOG)
              .order_by(LeadActivitySchema.created_at.desc())
        )).scalars().all()
        for a in acts:
            call_count[a.lead_id] = call_count.get(a.lead_id, 0) + 1
            if a.lead_id not in latest_call:   # rows are newest-first
                latest_call[a.lead_id] = a

        # --- order rollup: count, gross, net, latest status/payment per lead ---
        order_agg: Dict[str, Dict[str, Any]] = {}
        orders = (await session.execute(
            db.select(CustomerOrderSchema)
              .where(CustomerOrderSchema.lead_id.in_(lead_ids), *order_date_conds)
              .order_by(CustomerOrderSchema.order_date.desc())
        )).scalars().all()
        for o in orders:
            agg = order_agg.setdefault(o.lead_id, {
                "count": 0, "gross": 0.0, "net": 0.0, "booked": 0.0, "status": None, "payment": None,
                "outlet_id": None, "delivery_id": None, "remarks": None,
            })
            agg["count"] += 1
            agg["gross"] += _num(o.gross_amount)
            agg["net"] += _num(o.total_amount)
            # Booked/placed revenue = gross - discount (the Daily Revenue tab's formula,
            # reports.py:1263,1281 `placed` CTE) — distinct from `net` (total_amount).
            agg["booked"] += _num(o.gross_amount) - _num(o.discount_applied)
            if agg["status"] is None:   # newest-first -> first seen is latest
                agg["status"] = o.order_status.value if o.order_status else None
                agg["payment"] = o.payment_method.value if o.payment_method else None
                agg["outlet_id"] = o.assigned_outlet_id
                agg["delivery_id"] = o.delivery_person_id
                agg["remarks"] = o.status_remarks or None

        # --- latest order's outlet + delivery-person names (batched) ---
        outlet_ids = {a["outlet_id"] for a in order_agg.values() if a.get("outlet_id")}
        delivery_ids = {a["delivery_id"] for a in order_agg.values() if a.get("delivery_id")}
        outlet_names: Dict[str, str] = {}
        if outlet_ids:
            for uid, name in (await session.execute(
                db.select(OutletSchema.uid, OutletSchema.outlet_name)
                  .where(OutletSchema.uid.in_(outlet_ids))
            )).all():
                outlet_names[uid] = name or ""
        delivery_names: Dict[str, str] = {}
        if delivery_ids:
            for uid, name in (await session.execute(
                db.select(UserSchema.uid, UserSchema.full_name)
                  .where(UserSchema.uid.in_(delivery_ids))
            )).all():
                delivery_names[uid] = name or ""

        # --- quantity: sum(order_items.quantity) per lead via one grouped query ---
        qty: Dict[str, int] = {}
        for lead_id, total_qty in (await session.execute(
            db.select(CustomerOrderSchema.lead_id, db.func.sum(OrderItemSchema.quantity))
              .join(OrderItemSchema, OrderItemSchema.order_id == CustomerOrderSchema.uid)
              .where(CustomerOrderSchema.lead_id.in_(lead_ids), *order_date_conds)
              .group_by(CustomerOrderSchema.lead_id)
        )).all():
            qty[lead_id] = int(total_qty or 0)

        rows: List[Dict[str, Any]] = []
        for l in leads:
            call = latest_call.get(l.uid)
            details = (call.details or {}) if call else {}
            agg = order_agg.get(l.uid, {})
            rows.append({
                "uid": l.uid,   # for the table's "open lead" link; not in the Excel export
                "prospect_id": l.lead_number,
                "lead_name": " ".join(p for p in (l.first_name, l.last_name) if p),
                "created_at": l.created_at.isoformat() if l.created_at else "",  # UTC ISO; UI formats to IST
                "owner": owners.get(l.owner_id, ""),
                "owner_email": owner_emails.get(l.owner_id, ""),
                "phone": l.mobile or "",   # the lead's own mobile, not the owner's
                "lead_stage": l.stage.value if l.stage else "",
                "lead_source": l.source.value if l.source else "",
                "disposition": _disposition_label(details, call.outcome if call else None),
                "sub_disposition": details.get("sub_disposition") or "",
                "call_attempt": _call_attempt_label(call.outcome if call else None),
                "outlet": outlet_names.get(agg.get("outlet_id"), "") if agg.get("outlet_id") else "",
                "delivery_guy": delivery_names.get(agg.get("delivery_id"), "") if agg.get("delivery_id") else "",
                "order_remarks": agg.get("remarks") or "",
                "orders": agg.get("count", 0),
                "quantity": qty.get(l.uid, 0),
                "gross": round(agg.get("gross", 0.0), 2),
                "net": round(agg.get("net", 0.0), 2),
                "booked_rev": round(agg.get("booked", 0.0), 2),
                "avg_booked_rev_per_day": round(agg.get("booked", 0.0) / _pivot_days(from_date, to_date), 2),
                "order_status": agg.get("status") or "",
                "mode_of_payment": agg.get("payment") or "",
                "call_attempts": call_count.get(l.uid, 0),
                "region": (l.state or "").title(),   # stored lowercase-canonical; Title Case for display
            })
        return rows, total


async def agent_performance(
    engine, *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    order_from_date: Optional[date] = None,
    order_to_date: Optional[date] = None,
    region: Optional[str] = None,
    regions: Optional[List[str]] = None,
    owner_ids: Optional[List[str]] = None,
    source: Optional[str] = None,
    scope_owner_id=None,
    agency_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Per-owner lead-stage pivot + order rollup — the LSQ-style "agent performance"
    sheet. The three percentages are pure stage ratios (no call data):
      attempted% = (total − New Lead) / total
      connected% = (total − New Lead − Not Reachable) / total
      conversion% = (FTU + RTU) / total
    Lead-created date + region(s) + owner + scope filter which leads are counted; an
    order-date window scopes ONLY the order rollup columns (never drops an owner)."""
    conds, order_date_conds = _lead_filter_conds(
        from_date=from_date, to_date=to_date,
        order_from_date=order_from_date, order_to_date=order_to_date,
        region=region, regions=regions, owner_ids=owner_ids, source=source,
        scope_owner_id=scope_owner_id, agency_id=agency_id, gate_order_date=False)
    order_conds = list(conds) + list(order_date_conds)

    lm = LeadManager(engine)
    async with lm.session_factory() as session:
        # Stage counts per owner (drives the pivot + all three percentages).
        stage_rows = (await session.execute(
            db.select(LeadSchema.owner_id, LeadSchema.stage, db.func.count())
              .where(*conds).group_by(LeadSchema.owner_id, LeadSchema.stage)
        )).all()
        # Order rollup per TELECALLER (the order's creator, not the lead owner) —
        # order-level (no item fan-out): count / gross / net. Join to leads is kept so
        # the region/lead-created/scope filters still gate which orders count.
        ord_rows = (await session.execute(
            db.select(CustomerOrderSchema.telecaller_id,
                      db.func.count(CustomerOrderSchema.uid),
                      db.func.coalesce(db.func.sum(CustomerOrderSchema.gross_amount), 0),
                      db.func.coalesce(db.func.sum(CustomerOrderSchema.total_amount), 0))
              .select_from(LeadSchema)
              .join(CustomerOrderSchema, CustomerOrderSchema.lead_id == LeadSchema.uid)
              .where(*order_conds).group_by(CustomerOrderSchema.telecaller_id)
        )).all()
        # Quantity per telecaller — item-level, in its own query so summing items can't
        # fan out the gross/net above.
        qty_rows = (await session.execute(
            db.select(CustomerOrderSchema.telecaller_id, db.func.coalesce(db.func.sum(OrderItemSchema.quantity), 0))
              .select_from(LeadSchema)
              .join(CustomerOrderSchema, CustomerOrderSchema.lead_id == LeadSchema.uid)
              .join(OrderItemSchema, OrderItemSchema.order_id == CustomerOrderSchema.uid)
              .where(*order_conds).group_by(CustomerOrderSchema.telecaller_id)
        )).all()
        # People to show: lead owners + telecallers who created in-window orders.
        people = {r[0] for r in stage_rows if r[0]} | {r[0] for r in ord_rows if r[0]}
        owners: Dict[str, str] = {}
        # owner_id -> agency name (owner_id -> users.agency_id -> agencies.name), one
        # LEFT JOIN alongside the owner-name lookup above — no extra round trip (T2.1).
        agency_names: Dict[str, Optional[str]] = {}
        if people:
            for uid, name, email, agency_name in (await session.execute(
                db.select(UserSchema.uid, UserSchema.full_name, UserSchema.email, AgencySchema.name)
                  .select_from(UserSchema)
                  .outerjoin(AgencySchema, AgencySchema.uid == UserSchema.agency_id)
                  .where(UserSchema.uid.in_(people))
            )).all():
                owners[uid] = name or email or uid
                agency_names[uid] = agency_name

    ord_map = {r[0]: (int(r[1] or 0), float(r[2] or 0), float(r[3] or 0)) for r in ord_rows}
    qty_map = {r[0]: int(r[1] or 0) for r in qty_rows}
    all_stages = [s.value for s in LeadStage]
    per: Dict[Any, Dict[str, int]] = {}
    for owner_id, stage, cnt in stage_rows:
        sv = stage.value if hasattr(stage, "value") else stage
        per.setdefault(owner_id, {})[sv] = per.setdefault(owner_id, {}).get(sv, 0) + int(cnt)
    # Telecallers who created orders but own no in-window leads still get a row.
    for tid in ord_map:
        per.setdefault(tid, {})

    rows: List[Dict[str, Any]] = []
    for owner_id, counts in per.items():
        total = sum(counts.values())
        new_lead = counts.get(LeadStage.NEW_LEAD.value, 0)
        not_reach = counts.get(LeadStage.NOT_REACHABLE.value, 0)
        conv = counts.get(LeadStage.FTU.value, 0) + counts.get(LeadStage.RTU.value, 0)
        oc, gross, net = ord_map.get(owner_id, (0, 0.0, 0.0))
        rows.append({
            "owner_id": owner_id,
            "owner": owners.get(owner_id) or ("Unassigned" if not owner_id else owner_id),
            "agency_name": agency_names.get(owner_id),
            "stages": {sv: counts.get(sv, 0) for sv in all_stages},
            "total": total,
            "attempted_pct": round(100 * (total - new_lead) / total, 1) if total else 0.0,
            "connected_pct": round(100 * (total - new_lead - not_reach) / total, 1) if total else 0.0,
            "conversion_pct": round(100 * conv / total, 1) if total else 0.0,
            "order_count": oc,
            "order_quantity": qty_map.get(owner_id, 0),
            "gross": round(gross, 2),
            "net": round(net, 2),
        })
    rows.sort(key=lambda r: r["conversion_pct"])  # match the sheet (ascending by conv%)
    return rows


# --- State × stage pivot ("Prospect Pivot" dashboard) -----------------------
# Same three stage ratios as agent_performance, but pivoted per lead-inflow REGION
# (2026-07-16: stakeholder asked for regions, not one-column-per-state — most state
# columns sat nearly empty) with Grand Total, Not-Connected%, and Avg Lead/Day rows —
# the Google-Sheet pivot the business uses.

_PIVOT_STAGES = [s.value for s in LeadStage]  # row order = enum order

# Region display labels, keyed to tracker_queries.REGIONS's codes (single source of
# truth for STATE MEMBERSHIP — do not re-list states here). "ALL" has no state list
# (it means "no filter" over in tracker_queries) so it has no display label and is
# skipped when building the reverse map below.
_REGION_DISPLAY = {"KN": "Karnataka", "AP_TG": "AP & Telangana", "PN": "Punjab"}

# canon_state(...) (lowercase canonical state) -> region display label. Built once
# from REGIONS so this module never carries its own copy of the state lists; a state
# missing from REGIONS (Haryana, UP, Maharashtra, TN, Rajasthan, Uttarakhand, a raw
# district name that canon_state passes through unmatched, ...) falls through to
# "Others" in _state_label below, not KeyError.
def _build_state_to_region() -> Dict[str, str]:
    from services.tracker_queries import REGIONS
    return {
        state: code
        for code, states in REGIONS.items()
        if states is not None
        for state in states
    }


_STATE_TO_REGION = _build_state_to_region()


def _state_label(state: Optional[str]) -> str:
    """Canonical REGION label for the pivot column (Karnataka / AP & Telangana / Punjab /
    Others), folding casing/misspellings via ``canon_state`` first so dirty legacy rows
    land in the right region instead of spawning duplicate columns. ``None``/empty/pure-
    garbage raw values collapse to "Unknown" (existing catch-all, stays pinned first); a
    real value that isn't in any region (another Indian state, or a district name
    canon_state passes through unmatched) collapses to "Others".

    Telangana and Andhra Pradesh fold into the SAME "AP & Telangana" column here — this
    is a deliberate, explicit, LABELLED region grouping (stakeholder-requested), not the
    old silent revenue-hiding fold this module's docstring warns against elsewhere.
    ``canon_state(..., apply_alias=False)`` still runs underneath, so the state-level
    truth (Telangana as its own state) stays intact for every other consumer — this
    function only changes what the PIVOT DISPLAYS. Feeds both the lead-count side
    (state_stage_pivot) and _order_state_aggregate's ORDER/REVENUE rows, so AP + Telangana
    revenue sums into one column too. Lazy import dodges the crmReportService<->
    leadService circular import (same idiom as routers/v1/orders.py::_canon_order_state)."""
    from services.leadService import canon_state
    canon = canon_state(state, apply_alias=False)
    if not canon:
        return "Unknown"
    region_code = _STATE_TO_REGION.get(canon)
    return _REGION_DISPLAY[region_code] if region_code else "Others"


def _pct(numer: float, denom: float) -> float:
    return round(100 * numer / denom, 1) if denom else 0.0


def _pivot_days(from_date: Optional[date], to_date: Optional[date]) -> int:
    # Avg Lead/Day divides by the window's inclusive day count; default 1 (no window)
    # so it degrades to "leads in total" rather than dividing by zero.
    if from_date and to_date:
        return max(1, (to_date - from_date).days + 1)
    return 1


def build_state_pivot(counts: Dict[Tuple[str, str], int],
                      from_date: Optional[date] = None,
                      to_date: Optional[date] = None,
                      order_by_state: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Assemble the SS-shaped pivot from a ``(column_label, stage_value) -> count`` map.
    Columns = labels present, ordered Unknown, then the fixed region order (Karnataka,
    AP & Telangana, Punjab), then Others, then any leftover label alphabetically (that
    last bucket is dead code for the state pivot's own labels — _state_label only ever
    emits the 5 fixed names — but keeps this generic for `call_direction_pivot`, whose
    columns are ISO call-dates, not states/regions: none of those match the fixed names,
    so they fall into the alphabetical leftover bucket unchanged, i.e. plain chronological
    order exactly like before this fixed list existed) + Grand Total; rows = the stages
    present (enum order) then the derived %/avg rows. Percentages are stage ratios over
    each column's own Grand Total, identical to agent_performance.

    `order_by_state` is optional: a ``column_label -> {"orders", "booked", "qty"}`` map
    (see `_order_state_aggregate`). When given (the state pivot's case), 4 more rows are
    appended after "Avg Lead/Day": No. of Orders / Total Qty / Booked Revenue / Avg
    Booked Rev/Day, and its labels are unioned into the column set so an order-only
    region (no leads in the window) still gets a column. When None (the call pivot's
    case — `call_direction_pivot` never passes this), no order rows are added at all,
    columns are unaffected, and behavior is identical to before this param existed."""
    days = _pivot_days(from_date, to_date)
    present = {s for (s, _stg) in counts}
    has_order_rows = order_by_state is not None  # distinguishes "passed but empty" from "never passed"
    order_by_state = order_by_state or {}
    all_states = present | set(order_by_state.keys())
    fixed_order = ["Unknown", *_REGION_DISPLAY.values(), "Others"]
    state_cols = [c for c in fixed_order if c in all_states] + \
        sorted(s for s in all_states if s not in fixed_order)
    columns = state_cols + ["Grand Total"]

    def cell(col: str, stage: str) -> int:
        if col == "Grand Total":
            return sum(counts.get((s, stage), 0) for s in state_cols)
        return counts.get((col, stage), 0)

    stages = [stg for stg in _PIVOT_STAGES if any(cell(c, stg) for c in columns)]
    totals = {c: sum(cell(c, stg) for stg in stages) for c in columns}

    rows: List[Dict[str, Any]] = [
        {"label": stg, "type": "count", "values": {c: cell(c, stg) for c in columns if cell(c, stg)}}
        for stg in stages
    ]
    rows.append({"label": "Grand Total", "type": "total", "values": totals})

    NEW, NR = LeadStage.NEW_LEAD.value, LeadStage.NOT_REACHABLE.value
    FTU, RTU = LeadStage.FTU.value, LeadStage.RTU.value
    NQ = LeadStage.NOT_QUALIFIED.value

    def metric(label, kind, fn):
        return {"label": label, "type": kind, "values": {c: fn(c, totals[c]) for c in columns}}

    rows.append(metric("Attempted %", "pct", lambda c, T: _pct(T - cell(c, NEW), T)))
    rows.append(metric("Lead to Connected %", "pct", lambda c, T: _pct(T - cell(c, NEW) - cell(c, NR), T)))
    rows.append(metric("Lead to Conv%", "pct", lambda c, T: _pct(cell(c, FTU) + cell(c, RTU), T)))
    rows.append(metric("Not Connected", "warn", lambda c, T: _pct(cell(c, NR), T)))
    rows.append(metric("Not Qualified %", "warn", lambda c, T: _pct(cell(c, NQ), T)))
    rows.append(metric("Avg Lead/Day", "num", lambda c, T: round(T / days)))

    if has_order_rows:
        def ocell(col: str, field: str):
            if col == "Grand Total":
                return sum(order_by_state.get(s, {}).get(field, 0) for s in state_cols)
            return order_by_state.get(col, {}).get(field, 0)

        orders_vals = {c: ocell(c, "orders") for c in columns}
        qty_vals = {c: ocell(c, "qty") for c in columns}
        # Booked Revenue summed at full precision (see ocell's Grand Total sum-of-raw-
        # per-state values), rounded to cents only here for display — matches
        # prospect_report's `round(booked, 2)` convention for the same figure.
        booked_vals = {c: round(ocell(c, "booked"), 2) for c in columns}

        # 0/absent -> blank, same convention as the stage-count rows above.
        rows.append({"label": "No. of Orders", "type": "num",
                     "values": {c: v for c, v in orders_vals.items() if v}})
        rows.append({"label": "Total Qty", "type": "num",
                     "values": {c: v for c, v in qty_vals.items() if v}})
        rows.append({"label": "Booked Revenue", "type": "currency",
                     "values": {c: v for c, v in booked_vals.items() if v}})
        rows.append({"label": "Avg Booked Rev/Day", "type": "currency",
                     "values": {c: round(v / days) for c, v in booked_vals.items() if v}})

    return {"columns": columns, "rows": rows, "days": days}


async def _order_state_aggregate(
    session, *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    scope_owner_id=None,
    agency_id: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Per-state order aggregate for the Prospect Pivot's 4 order rows — mirrors the
    Daily-Rev `placed` CTE (app/routers/v1/reports.py:1261-1285) EXACTLY so the pivot's
    Grand Total ties out to Daily-Rev's "Placed" figures:
      - `orders` = COUNT(uid), `booked` = SUM(gross_amount - discount_applied), filtered
        to `created_at` IST-in-window (`_day_bounds`, same helper the rest of this module
        uses) — NO order_status filter (placed = every order created in the window).
      - NO deleted_at filter either: the `placed` CTE (raw SQL against customer_orders)
        has none, so adding `CustomerOrderSchema.deleted_at.is_(None)` here would silently
        diverge the two totals whenever a soft-deleted order falls in the window. If
        Daily-Rev is ever changed to exclude soft-deleted orders, this must change too.
      - `qty` = SUM(order_items.quantity) via a SEPARATE grouped query (same idiom as
        prospect_report's qty rollup, crmReportService.py:291-299) — joining order_items
        into the same query as the gross/discount SUM would fan out one row per item and
        inflate `booked` for any multi-item order.
    Keyed by `_state_label(order.state)` (additive: two raw spellings that fold to the
    same label are summed together, same as the lead-count side).

    Scope: when `scope_owner_id`/`agency_id` is set, orders are joined to `leads` and
    restricted via `_owner_scope_conds` — the exact same scope logic `_lead_filter_conds`
    applies to the lead side, so an agency admin's order rows match their lead rows'
    scope. When BOTH are None (superadmin/all-agencies — the case that must match
    Daily-Rev), NO lead join happens and every order in the window counts, because
    Daily-Rev's `placed` CTE has no owner/agency scoping at all — adding one here would
    break the tie-out for exactly the case the acceptance criterion is about.
    """
    gte, lte = _day_bounds(from_date, to_date)
    window_conds = []
    if gte is not None:
        window_conds.append(CustomerOrderSchema.created_at >= gte)
    if lte is not None:
        window_conds.append(CustomerOrderSchema.created_at <= lte)

    scope_conds = _owner_scope_conds(scope_owner_id, agency_id)
    conds = [*window_conds, *scope_conds]

    orders_q = (db.select(
        CustomerOrderSchema.state,
        db.func.count(CustomerOrderSchema.uid),
        db.func.sum(CustomerOrderSchema.gross_amount - CustomerOrderSchema.discount_applied),
    ).select_from(CustomerOrderSchema))
    qty_q = (db.select(
        CustomerOrderSchema.state,
        db.func.sum(OrderItemSchema.quantity),
    ).select_from(CustomerOrderSchema)
     .join(OrderItemSchema, OrderItemSchema.order_id == CustomerOrderSchema.uid))

    if scope_conds:
        orders_q = orders_q.join(LeadSchema, LeadSchema.uid == CustomerOrderSchema.lead_id)
        qty_q = qty_q.join(LeadSchema, LeadSchema.uid == CustomerOrderSchema.lead_id)

    orders_q = orders_q.where(*conds).group_by(CustomerOrderSchema.state)
    qty_q = qty_q.where(*conds).group_by(CustomerOrderSchema.state)

    agg: Dict[str, Dict[str, Any]] = {}
    for state, cnt, booked in (await session.execute(orders_q)).all():
        a = agg.setdefault(_state_label(state), {"orders": 0, "booked": 0.0, "qty": 0})
        a["orders"] += int(cnt or 0)
        a["booked"] += _num(booked)
    for state, qty in (await session.execute(qty_q)).all():
        a = agg.setdefault(_state_label(state), {"orders": 0, "booked": 0.0, "qty": 0})
        a["qty"] += int(qty or 0)
    return agg


async def state_stage_pivot(
    engine, *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    source: Optional[str] = None,
    scope_owner_id=None,
    agency_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Count leads created in the window per (state, stage), grouped into REGION columns
    via `_state_label` (Karnataka / AP & Telangana / Punjab / Others / Unknown — see that
    function's docstring). Also aggregates `customer_orders` per-region
    (`_order_state_aggregate`) and passes it to `build_state_pivot` as `order_by_state`,
    adding the 4 order/revenue rows — unlike `call_direction_pivot`, which calls
    `build_state_pivot` without this param and so never gets those rows."""
    conds, _ = _lead_filter_conds(
        from_date=from_date, to_date=to_date, source=source,
        scope_owner_id=scope_owner_id, agency_id=agency_id, gate_order_date=False)
    lm = LeadManager(engine)
    async with lm.session_factory() as session:
        rows = (await session.execute(
            db.select(LeadSchema.state, LeadSchema.stage, db.func.count())
              .where(*conds).group_by(LeadSchema.state, LeadSchema.stage)
        )).all()
        order_by_state = await _order_state_aggregate(
            session, from_date=from_date, to_date=to_date,
            scope_owner_id=scope_owner_id, agency_id=agency_id)
    counts: Dict[Tuple[str, str], int] = {}
    for state, stage, cnt in rows:
        sv = stage.value if hasattr(stage, "value") else stage
        counts[(_state_label(state), sv)] = counts.get((_state_label(state), sv), 0) + int(cnt)
    return build_state_pivot(counts, from_date, to_date, order_by_state=order_by_state)


# --- Call Log report (Task B2: SA/AA cross-lead call-log view) --------------
# Flat list of activity rows (default CALL_LOG) across the caller's scope, newest
# first — unlike the reports above, the date window here gates the ACTIVITY's
# created_at (when the call happened), not the lead's created_at, so the same
# `_day_bounds` window means "calls logged in this range" not "leads created in
# this range".

# ponytail: hard cap so a broad filter (or none) never pulls the whole activity
# table into memory on the single-task box; paginate if this ever needs to grow
# past a screenful. `truncated` tells the caller more rows were cut off.
CALL_LOG_LIMIT = 5000


async def call_log_activities(
    engine, *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    direction: Optional[str] = None,
    outcome: Optional[str] = None,
    activity_type: Optional[str] = None,
    owner_ids: Optional[List[str]] = None,
    scope_owner_id=None,
    agency_id: Optional[str] = None,
    limit: int = CALL_LOG_LIMIT,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Return (rows, truncated). Rows are lead_activities joined to their lead,
    restricted to the SAME scope the other reports use (`scope_owner_id` /
    `agency_id` via `_lead_filter_conds`) plus the explicit `owner_ids` filter.
    `direction` compares the JSON `details->>'direction'`; `outcome` compares the
    plain `outcome` column. Defaults to `LeadActivityType.CALL_LOG` when
    `activity_type` is not given."""
    at = _enum_or_raw(LeadActivityType, activity_type) if activity_type else LeadActivityType.CALL_LOG

    # Scope-only conds (no lead-created-at window — that's not what this report
    # filters by); reuses the exact same owner/agency scoping the other reports do.
    lead_conds, _ = _lead_filter_conds(
        owner_ids=owner_ids, scope_owner_id=scope_owner_id, agency_id=agency_id,
        gate_order_date=False)

    conds = [LeadActivitySchema.activity_type == at, *lead_conds]
    gte, lte = _day_bounds(from_date, to_date)
    if gte is not None:
        conds.append(LeadActivitySchema.created_at >= gte)
    if lte is not None:
        conds.append(LeadActivitySchema.created_at <= lte)
    if direction:
        conds.append(LeadActivitySchema.details["direction"].as_string() == direction)
    if outcome:
        conds.append(LeadActivitySchema.outcome == outcome)

    lm = LeadManager(engine)
    async with lm.session_factory() as session:
        pairs = (await session.execute(
            db.select(LeadActivitySchema, LeadSchema)
              .select_from(LeadActivitySchema)
              .join(LeadSchema, LeadActivitySchema.lead_id == LeadSchema.uid)
              .where(*conds)
              .order_by(LeadActivitySchema.created_at.desc())
              .limit(limit + 1)
        )).all()

        truncated = len(pairs) > limit
        pairs = pairs[:limit]

        owner_ids_present = {l.owner_id for (_a, l) in pairs if l.owner_id}
        owners: Dict[str, str] = {}
        if owner_ids_present:
            for uid, name, email in (await session.execute(
                db.select(UserSchema.uid, UserSchema.full_name, UserSchema.email)
                  .where(UserSchema.uid.in_(owner_ids_present))
            )).all():
                owners[uid] = name or email or uid

        rows: List[Dict[str, Any]] = []
        for a, l in pairs:
            details = a.details or {}
            rows.append({
                "activity_id": a.uid,
                "created_at": a.created_at.isoformat() if a.created_at else "",
                "lead_id": l.uid,
                "lead_number": l.lead_number,
                "lead_name": " ".join(p for p in (l.first_name, l.last_name) if p),
                "owner_id": l.owner_id,
                "owner_name": owners.get(l.owner_id, ""),
                "direction": details.get("direction") or "",
                "outcome": a.outcome or "",
                "disposition": _disposition_label(details, a.outcome),
                "duration_seconds": details.get("duration_seconds"),
            })
        return rows, truncated


# --- Inbound/outbound call pivot (Task C1) -----------------------------------
# Same stage x column pivot as state_stage_pivot (via build_state_pivot), but
# columns are ISO call-dates instead of states, and the window gates the CALL's
# created_at (like call_log_activities) — not the lead's created_at. A lead
# counts once per (call-day, its CURRENT stage), even with multiple calls that
# day in that direction ("unique inflow").
#
# ponytail: aggregates the distinct-per-day count in Python over the raw
# activity+lead join (same query shape as call_log_activities) rather than a SQL
# COUNT(DISTINCT ...) GROUP BY — keeps the dedup logic a plain Python set, DB-free
# testable, and fine at the report's day/week window scale.

async def call_direction_pivot(
    engine, *,
    direction: str,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    scope_owner_id=None,
    agency_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Stage x call-date pivot for one call direction ("inbound"/"outbound") — the
    inbound/outbound call pivot the business uses. `direction` compares the JSON
    `details->>'direction'`, same idiom as call_log_activities. The date window
    gates the call's created_at (when it happened, IST), not the lead's created_at."""
    # Scope-only conds (own leads / agency roster) — no lead-created-at window,
    # same split call_log_activities uses.
    lead_conds, _ = _lead_filter_conds(
        scope_owner_id=scope_owner_id, agency_id=agency_id, gate_order_date=False)

    conds = [
        LeadActivitySchema.activity_type == LeadActivityType.CALL_LOG,
        LeadActivitySchema.details["direction"].as_string() == direction,
        *lead_conds,
    ]
    gte, lte = _day_bounds(from_date, to_date)
    if gte is not None:
        conds.append(LeadActivitySchema.created_at >= gte)
    if lte is not None:
        conds.append(LeadActivitySchema.created_at <= lte)

    lm = LeadManager(engine)
    async with lm.session_factory() as session:
        pairs = (await session.execute(
            db.select(LeadActivitySchema, LeadSchema)
              .select_from(LeadActivitySchema)
              .join(LeadSchema, LeadActivitySchema.lead_id == LeadSchema.uid)
              .where(*conds)
        )).all()

    # Unique lead per (call-day, stage): a lead calling twice the same day in the
    # same direction still counts once — "unique inflow" like the sheet.
    seen: Dict[Tuple[str, str], set] = {}
    for a, l in pairs:
        if a.created_at is None:
            continue
        iso_day = a.created_at.astimezone(IST).date().isoformat()
        stage = l.stage.value if hasattr(l.stage, "value") else l.stage
        seen.setdefault((iso_day, stage), set()).add(l.uid)

    counts = {key: len(uids) for key, uids in seen.items()}
    return build_state_pivot(counts, from_date, to_date)
