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
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple

import sqlalchemy as db

from managers import (
    LeadManager, LeadSchema, LeadActivitySchema,
    CustomerOrderSchema, OrderItemSchema, UserSchema, OutletSchema,
)
from utils.crm_enums import LeadActivityType, CallOutcome, LeadStage
from utils.crm_constants import LeadSource


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


def _day_bounds(from_date: Optional[date], to_date: Optional[date]):
    """Inclusive calendar-day bounds as UTC datetimes (lead created_at basis)."""
    gte = datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc) if from_date else None
    lte = datetime.combine(to_date, datetime.max.time(), tzinfo=timezone.utc) if to_date else None
    return gte, lte


def _num(value) -> float:
    return float(value) if isinstance(value, Decimal) else (value or 0)


async def prospect_report(
    engine, *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    region: Optional[str] = None,
    owner_id: Optional[str] = None,
    stage: Optional[str] = None,
    source: Optional[str] = None,
    lead_numbers: Optional[List[str]] = None,
    scope_owner_id: Optional[str] = None,
    extra_clause=None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Return (rows, total). `limit=0` means "all matched rows" (capped at
    MAX_EXPORT_ROWS) — used by the CSV export. `scope_owner_id` restricts to one
    owner's leads (non-global callers); None = all leads (superadmin).
    `extra_clause` is an optional pre-built SQLAlchemy boolean clause (from the
    advanced filter engine) AND-ed into the same `conds` used by both queries."""
    lm = LeadManager(engine)
    async with lm.session_factory() as session:
        conds = [LeadSchema.deleted_at.is_(None)]
        gte, lte = _day_bounds(from_date, to_date)
        if gte is not None:
            conds.append(LeadSchema.created_at >= gte)
        if lte is not None:
            conds.append(LeadSchema.created_at <= lte)
        if region:
            conds.append(LeadSchema.state == region)
        if owner_id:
            conds.append(LeadSchema.owner_id == owner_id)
        if stage:
            conds.append(LeadSchema.stage == _enum_or_raw(LeadStage, stage))
        if source:
            conds.append(LeadSchema.source == _enum_or_raw(LeadSource, source))
        if lead_numbers:
            conds.append(LeadSchema.lead_number.in_(lead_numbers))
        if scope_owner_id is not None:
            conds.append(LeadSchema.owner_id == scope_owner_id)
        if extra_clause is not None:
            conds.append(extra_clause)

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
              .where(CustomerOrderSchema.lead_id.in_(lead_ids))
              .order_by(CustomerOrderSchema.order_date.desc())
        )).scalars().all()
        for o in orders:
            agg = order_agg.setdefault(o.lead_id, {
                "count": 0, "gross": 0.0, "net": 0.0, "status": None, "payment": None,
                "outlet_id": None, "delivery_id": None, "remarks": None,
            })
            agg["count"] += 1
            agg["gross"] += _num(o.gross_amount)
            agg["net"] += _num(o.total_amount)
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
              .where(CustomerOrderSchema.lead_id.in_(lead_ids))
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
                "order_status": agg.get("status") or "",
                "mode_of_payment": agg.get("payment") or "",
                "call_attempts": call_count.get(l.uid, 0),
                "region": l.state or "",
            })
        return rows, total
