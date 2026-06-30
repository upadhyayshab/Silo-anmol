"""Lead service (Feature 1) — Stage 1.

Centralizes every lead mutation so that the activity timeline is written
consistently from one place. Routers stay thin and just translate HTTP.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List, Tuple

import sqlalchemy as db
from sqlalchemy.exc import IntegrityError

from managers import (
    LeadManager, LeadSchema,
    LeadActivityManager, LeadActivitySchema,
    LeadAssignmentSchema,
    UserManager, OutletManager,
)
from models import (
    LeadResponse, LeadDetailResponse, LeadActivityResponse,
    TodayQueueBucket, TodayQueueResponse,
)
from utils.constants import UserRole
from utils.crm_enums import (LeadStage, LeadActivityType, AssignmentReason,
                             DISPOSITION_OUTCOME, DNC_SUB_DISPOSITIONS)
from utils import dedup_utils
from services import assignmentService

logger = logging.getLogger(__name__)

# India Standard Time — the business is India-wide, so "today" for the callback
# queue is bucketed on the IST calendar day.
IST = timezone(timedelta(hours=5, minutes=30))


# Fields a telecaller/admin may edit via PATCH (stage & owner are excluded — they
# have dedicated endpoints that log richer timeline entries).
EDITABLE_FIELDS = {
    "first_name", "last_name", "mobile", "phone", "email",
    "address_line", "address_line_2", "city", "district", "state", "pincode", "country",
    "source", "lead_score", "follow_up_at",
    "do_not_call", "do_not_sms", "do_not_email",
    "custom_fields", "campaign_data", "notes",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gen_lead_number() -> str:
    return f"LEAD-{uuid.uuid4().hex[:10].upper()}"


# Pseudo-actors that are not rows in `users` (must not be written to FK columns).
_NON_USER_ACTORS = {"system", "microservice", None, ""}


def _real_user(actor: Optional[str]) -> Optional[str]:
    """Return a real users.uid, or None for system/webhook/microservice actors."""
    return actor if actor not in _NON_USER_ACTORS else None


def _fire_capi(lead, stage) -> None:
    """Best-effort: report a lead stage event to Meta CAPI without blocking the caller."""
    try:
        from services import facebook_capi
        asyncio.create_task(facebook_capi.send_stage_event(lead, stage))
    except RuntimeError:
        pass  # no running event loop (e.g. a sync script) — skip
    except Exception as e:
        logger.warning(f"[capi] could not enqueue stage event for "
                       f"{getattr(lead, 'uid', None)}: {e}")


# --------------------------------------------------------------------------
# Role scoping (checkpoint 1.4)
# --------------------------------------------------------------------------

def scope_filters_for_user(user) -> Dict[str, Any]:
    """Filters that restrict a list query to what `user` may see."""
    if user.role in (UserRole.SUPER_ADMIN, UserRole.ADMIN):
        return {}
    # Telecaller: only their own leads.
    return {"owner_id": user.uid}


def user_can_access(user, lead) -> bool:
    if user.role in (UserRole.SUPER_ADMIN, UserRole.ADMIN):
        return True
    return lead.owner_id == user.uid


# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------

async def record_activity(engine, lead_id: str, activity_type: LeadActivityType, *,
                          user_id: Optional[str] = None, body: Optional[str] = None,
                          outcome: Optional[str] = None, from_stage: Optional[str] = None,
                          to_stage: Optional[str] = None,
                          details: Optional[Dict[str, Any]] = None) -> LeadActivitySchema:
    activity_manager = LeadActivityManager(engine)
    activity = await activity_manager.create(LeadActivitySchema(
        lead_id=lead_id,
        user_id=_real_user(user_id),  # never write "system"/"microservice" to the users FK
        activity_type=activity_type,
        body=body,
        outcome=outcome,
        from_stage=from_stage,
        to_stage=to_stage,
        details=details,
    ))
    # Keep the lead's last-activity marker fresh (used by the Stage 2 queue).
    try:
        await LeadManager(engine).update(lead_id, {"last_activity_at": _now()})
    except Exception as e:
        logger.warning(f"[lead] could not bump last_activity_at for {lead_id}: {e}")
    return activity


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------

async def create_lead(engine, payload, by_user_id: str,
                      *, source_label: Optional[str] = None) -> Tuple[LeadSchema, bool]:
    """Create a lead, or merge it into an existing duplicate.

    Returns ``(lead, created)`` — ``created`` is ``False`` when an existing
    (non-deleted) lead matched on phone/email and the incoming data was merged
    into it instead of inserting a new row. Dedup runs on **every** create path
    (manual API, FB webhook, CSV import).

    Ownership rules (only when a new lead is created):
      1. explicit ``payload.owner_id``  -> assigned to that telecaller (manual)
      2. created by a real user (telecaller/admin via API) -> attributed to the creator
      3. system / webhook (no human creator) -> round-robin within the lead's region

    Admins redistribute later via ``distribute_leads`` / the assign endpoint.
    """
    lead_manager = LeadManager(engine)

    creator = _real_user(by_user_id)  # None for system/webhook/microservice

    # --- Deduplication: merge into an existing non-deleted lead if one matches.
    existing = await dedup_utils.find_duplicate(
        engine, mobile=payload.mobile, email=payload.email
    )
    if existing is not None:
        label = source_label or (
            payload.source.value if getattr(payload, "source", None) else None
        ) or "manual entry"
        merged = await dedup_utils.merge_into_existing(
            engine, existing, payload.model_dump(),
            source_label=label, by_user_id=by_user_id,
        )
        return merged, False

    outlet = await assignmentService.resolve_outlet(
        engine, district=payload.district, pincode=payload.pincode, state=payload.state
    )
    outlet_id = outlet.uid if outlet else None
    # Authoritative region: the resolved outlet's state, else the lead's own state.
    region_state = (getattr(outlet, "state", None) if outlet else None) or payload.state

    if payload.owner_id:
        owner_id = payload.owner_id
        reason = AssignmentReason.MANUAL.value
    elif creator:
        # Attribute the lead to whoever created it.
        owner_id = creator
        reason = AssignmentReason.SELF_CREATED.value
    else:
        picked = await assignmentService.pick_telecaller(engine, outlet_id, region_state)
        owner_id = picked.uid if picked else None
        reason = AssignmentReason.ROUND_ROBIN.value

    lead = LeadSchema(
        first_name=payload.first_name,
        last_name=payload.last_name,
        # Store the canonical bare-digit mobile so the column is clean for
        # downstream dialing/search; fall back to the raw value if it has no
        # digits. (Matching still tolerates legacy formats via find_duplicate.)
        mobile=dedup_utils.normalize_mobile(payload.mobile) or payload.mobile,
        phone=payload.phone,
        email=payload.email,
        address_line=payload.address_line,
        address_line_2=payload.address_line_2,
        city=payload.city,
        district=payload.district,
        state=payload.state,
        pincode=payload.pincode,
        country=payload.country,
        source=payload.source,
        lead_score=payload.lead_score,
        do_not_call=bool(getattr(payload, "do_not_call", None)),
        do_not_sms=bool(getattr(payload, "do_not_sms", None)),
        do_not_email=bool(getattr(payload, "do_not_email", None)),
        custom_fields=payload.custom_fields,
        campaign_data=payload.campaign_data,
        notes=payload.notes,
        lead_number=_gen_lead_number(),
        stage=LeadStage.NEW_LEAD,
        owner_id=owner_id,
        outlet_id=outlet_id,
        last_activity_at=_now(),
    )
    try:
        lead = await lead_manager.create(lead)
    except IntegrityError:
        # Lost a dedup race: a concurrent create (e.g. FB webhook + backfill
        # firing the same leadgen lead, ms apart) inserted this mobile first and
        # the uq_leads_mobile_active index rejected ours. Re-resolve the winner
        # (now committed) and merge into it instead of erroring.
        existing = await dedup_utils.find_duplicate(
            engine, mobile=payload.mobile, email=payload.email
        )
        if existing is None:
            raise  # not the mobile-dedup constraint — surface the real error
        label = source_label or (
            payload.source.value if getattr(payload, "source", None) else None
        ) or "manual entry"
        merged = await dedup_utils.merge_into_existing(
            engine, existing, payload.model_dump(),
            source_label=label, by_user_id=by_user_id,
        )
        return merged, False

    await record_activity(engine, lead.uid, LeadActivityType.CREATED, user_id=creator,
                          body=f"Lead {lead.lead_number} created")

    if owner_id:
        await assignmentService.record_assignment(
            engine, lead.uid, owner_id, reason=reason, assigned_by=(creator or "system"),
        )
        await record_activity(engine, lead.uid, LeadActivityType.ASSIGNMENT,
                              user_id=creator, body=f"Assigned to telecaller ({reason})",
                              details={"telecaller_id": owner_id, "reason": reason})

    final = await lead_manager.fetch(lead.uid)
    _fire_capi(final, LeadStage.NEW_LEAD)
    return final, True


async def update_lead(engine, lead: LeadSchema, changes: Dict[str, Any],
                      by_user_id: str) -> LeadSchema:
    """Apply an editable-field patch and log a single FIELD_UPDATE diff (1.5)."""
    lead_manager = LeadManager(engine)

    applied: Dict[str, Any] = {}
    diff: Dict[str, Any] = {}
    for field, new_value in changes.items():
        if field not in EDITABLE_FIELDS or new_value is None:
            continue
        old_value = getattr(lead, field, None)
        # Normalize enums to comparable/serializable values.
        old_cmp = old_value.value if hasattr(old_value, "value") else old_value
        new_cmp = new_value.value if hasattr(new_value, "value") else new_value
        if old_cmp == new_cmp:
            continue
        applied[field] = new_value
        diff[field] = {"from": _jsonable(old_cmp), "to": _jsonable(new_cmp)}

    if not applied:
        return lead

    updated = await lead_manager.update(lead.uid, applied)
    await record_activity(engine, lead.uid, LeadActivityType.FIELD_UPDATE,
                          user_id=by_user_id,
                          body=f"Updated {', '.join(diff.keys())}",
                          details=diff)
    return updated


async def change_stage(engine, lead: LeadSchema, new_stage: LeadStage,
                       by_user_id: Optional[str], note: Optional[str] = None) -> LeadSchema:
    lead_manager = LeadManager(engine)
    from_stage = lead.stage.value if hasattr(lead.stage, "value") else lead.stage
    to_stage = new_stage.value if hasattr(new_stage, "value") else new_stage

    updated = await lead_manager.update(lead.uid, {"stage": new_stage})
    await record_activity(engine, lead.uid, LeadActivityType.STAGE_CHANGE,
                          user_id=by_user_id, from_stage=from_stage, to_stage=to_stage,
                          body=note or f"Stage changed {from_stage} → {to_stage}")
    _fire_capi(updated, new_stage)
    return updated


async def handle_post_order(engine, lead_id: str, order_value: Decimal) -> None:
    """Handle CRM-side effects after an order is placed on a lead (checkpoint 3.3).
    Increments order_count and order_value. Auto-advances the stage to FTU or RTU.
    Attributed to 'system' so it doesn't pollute the telecaller's manual activity log.
    """
    lead_manager = LeadManager(engine)
    lead = await lead_manager.fetch(lead_id)
    if not lead:
        return

    new_count = lead.order_count + 1
    new_value = (lead.order_value or Decimal('0.00')) + order_value

    # Update counts
    await lead_manager.update(lead.uid, {
        "order_count": new_count,
        "order_value": new_value
    })

    # Determine auto-stage
    target_stage = None
    if new_count == 1:
        target_stage = LeadStage.FTU
    elif new_count > 1:
        target_stage = LeadStage.RTU

    # Auto-advance if it implies a change (and only if the lead is not in a terminal state maybe? 
    # For now, always push to FTU/RTU as requested).
    if target_stage and lead.stage != target_stage:
        await change_stage(engine, lead, target_stage, by_user_id=None, note=f"Auto-advanced to {target_stage.value} on order #{new_count}")


async def log_order_status_change(engine, order, new_status, by_user_id,
                                  *, old_status=None, remarks=None) -> None:
    """Log an order's status change on its linked lead's timeline (1 row).

    Attributed to the ERP user who made the change (by_user_id). No-op when the
    order isn't linked to a lead (e.g. legacy/LSQ-only orders).
    """
    lead_id = getattr(order, "lead_id", None)
    if not lead_id:
        return
    new_val = new_status.value if hasattr(new_status, "value") else str(new_status)
    old_val = old_status.value if hasattr(old_status, "value") else (old_status or None)
    body = f"Order {order.order_number} -> {new_val}"
    if remarks:
        body += f" — {remarks}"
    await record_activity(
        engine, lead_id, LeadActivityType.ORDER_UPDATE, user_id=by_user_id,
        body=body,
        details={"order_id": order.uid, "from": old_val, "to": new_val,
                 "remarks": remarks},
    )


async def add_note(engine, lead_id: str, body: str, by_user_id: str) -> LeadActivitySchema:
    return await record_activity(engine, lead_id, LeadActivityType.NOTE,
                                 user_id=by_user_id, body=body)


def _pick_inbound_autolog(items, call_sid: Optional[str]):
    """Pure pick: among recent CALL_LOG rows (most-recent-first), the un-dispositioned
    inbound auto-log to fold a disposition into — exact call_id match wins, else newest.
    None means no auto-log to reuse (insert a fresh row instead)."""
    autologs = [a for a in items
                if (getattr(a, "details", None) or {}).get("direction") == "inbound"
                and not (getattr(a, "details", None) or {}).get("disposition")]
    if call_sid:
        exact = next((a for a in autologs
                      if (a.details or {}).get("call_id") == call_sid), None)
        if exact:
            return exact
    return autologs[0] if autologs else None


async def _recent_inbound_autolog(engine, lead_id: str, call_sid: Optional[str]):
    """The Exotel webhook auto-logs every inbound call the moment it ends; this finds that
    row so the agent's later disposition can be folded into it (one call = one entry, the
    same principle as the outbound CDR fold). Returns the matching CALL_LOG, else None.

    ponytail: not guarded for the reverse race (agent dispositions BEFORE the ~1-2s webhook
    lands) — implausible (the picker's connected-prefill alone polls at 3s). If Exotel ever
    delays StatusCallback past the disposition, dedup the webhook side on call_id too."""
    recent = await LeadActivityManager(engine).fetch_all(
        limit=5, filters={"lead_id": lead_id, "activity_type": LeadActivityType.CALL_LOG},
        sorts=["created_at"])                      # "created_at" (no '-') => DESC, newest first
    return _pick_inbound_autolog(recent.items, call_sid)


async def log_call(engine, lead: LeadSchema, outcome: Optional[str], note: Optional[str],
                   follow_up_at: Optional[datetime], by_user_id: str,
                   duration_seconds: Optional[int] = None,
                   disposition: Optional[str] = None,
                   sub_disposition: Optional[str] = None,
                   *, direction: Optional[str] = None,
                   call_sid: Optional[str] = None) -> LeadActivitySchema:
    updates: Dict[str, Any] = {}
    if follow_up_at is not None:
        updates["follow_up_at"] = follow_up_at
    body = note
    # Disposition path: derive the outcome + side effects from the picked sub-disposition.
    if sub_disposition:
        outcome = DISPOSITION_OUTCOME.get(sub_disposition, outcome or "answered")
        if sub_disposition in DNC_SUB_DISPOSITIONS:
            updates["do_not_call"] = True
        label = f"{disposition} · {sub_disposition}" if disposition else sub_disposition
        body = f"{label} — {note}" if note else label
    if updates:
        await LeadManager(engine).update(lead.uid, updates)
    details: Dict[str, Any] = {}
    if duration_seconds is not None:
        details["duration_seconds"] = duration_seconds
    if sub_disposition:
        details.update({"disposition": disposition, "sub_disposition": sub_disposition})

    # Inbound is already auto-logged by the webhook the moment it ends; fold this
    # disposition INTO that row (one call = one timeline entry) and re-attribute it to the
    # agent who handled it. Outbound softphone fires no webhook, so it always inserts below.
    if direction == "inbound":
        autolog = await _recent_inbound_autolog(engine, lead.uid, call_sid)
        if autolog is not None:
            merged = {**(autolog.details or {}), **details}
            await LeadActivityManager(engine).update(autolog.uid, {
                "body": body, "outcome": outcome,
                "user_id": _real_user(by_user_id), "details": merged or None,
            })
            await LeadManager(engine).update(lead.uid, {"last_activity_at": _now()})
            return await LeadActivityManager(engine).fetch(autolog.uid)

    return await record_activity(engine, lead.uid, LeadActivityType.CALL_LOG,
                                 user_id=by_user_id, outcome=outcome, body=body,
                                 details=details or None)


async def attach_call_details(engine, activity_uid: str, patch: Dict[str, Any]) -> None:
    """Merge extra fields (recording URL, real duration) into a CALL_LOG activity's
    `details` JSON. Lets the softphone CDR — which settles a few seconds after hang-up —
    be folded into the disposition entry, so one call is one timeline row, not two."""
    activity_manager = LeadActivityManager(engine)
    activity = await activity_manager.fetch(activity_uid)
    details = dict(activity.details or {})
    details.update({k: v for k, v in patch.items() if v is not None})
    await activity_manager.update(activity_uid, {"details": details})


async def reassign(engine, lead: LeadSchema, telecaller_id: str, by_user_id: str,
                   reason: str = AssignmentReason.MANUAL.value) -> LeadSchema:
    """Change a lead's owner. Used by the assign endpoint (manual) and distribute (round_robin)."""
    lead_manager = LeadManager(engine)
    await assignmentService.record_assignment(
        engine, lead.uid, telecaller_id,
        reason=reason, assigned_by=(_real_user(by_user_id) or "system"),
    )
    updated = await lead_manager.update(lead.uid, {"owner_id": telecaller_id})
    await record_activity(engine, lead.uid, LeadActivityType.ASSIGNMENT, user_id=by_user_id,
                          body=f"Reassigned ({reason})",
                          details={"telecaller_id": telecaller_id, "reason": reason})
    return updated


async def distribute_leads(engine, lead_ids: List[str], telecaller_ids: Optional[List[str]],
                           by_user_id: str) -> Dict[str, Any]:
    """Bulk round-robin distribution of leads across telecallers (admin action).

    `telecaller_ids` selects the target pool; if omitted, all active telecallers are used.
    Returns a summary: how many were assigned/skipped and the per-telecaller counts.
    """
    user_manager = UserManager(engine)
    lead_manager = LeadManager(engine)
    from datetime import datetime, timezone
    
    now = datetime.now(timezone.utc)
    pool_objects = []
    current_loads = {}

    async def get_active_count(uid: str) -> int:
        open_leads = await lead_manager.fetch_all(filters={"owner_id": uid})
        # Terminal stages that don't count towards active quota
        terminal = ["Not Qualified", "Not Reachable", "Lapsed"]
        return sum(1 for l in open_leads.items if (l.stage.value if hasattr(l.stage, "value") else l.stage) not in terminal)

    async def add_to_pool(u):
        if u.role != UserRole.TELECALLER or not u.is_active:
            return
        # Filter offline telecallers (inactive > 30m)
        if not u.last_active_at or (now - u.last_active_at.replace(tzinfo=timezone.utc)).total_seconds() > 1800:
            return
        active_count = await get_active_count(u.uid)
        if u.assignment_quota and u.assignment_quota > 0 and active_count >= u.assignment_quota:
            return
        pool_objects.append(u)
        current_loads[u.uid] = active_count

    # Build the validated target pool
    if telecaller_ids:
        for tid in telecaller_ids:
            try:
                u = await user_manager.fetch(tid)
                await add_to_pool(u)
            except Exception:
                continue
    else:
        active = await user_manager.fetch_all(
            filters={"role": UserRole.TELECALLER, "is_active": True}
        )
        for u in active.items:
            await add_to_pool(u)

    if not pool_objects:
        return {"assigned": 0, "skipped": len(lead_ids or []), "by_telecaller": {},
                "detail": "no active/online telecallers available or all quotas full"}

    assigned, skipped, by_tc = 0, 0, {}
    
    for lid in lead_ids or []:
        # Re-evaluate pool to exclude those who just hit their quota
        valid_pool = [u for u in pool_objects if not (u.assignment_quota and u.assignment_quota > 0 and current_loads[u.uid] >= u.assignment_quota)]
        if not valid_pool:
            skipped += 1
            continue

        try:
            lead = await lead_manager.fetch(lid)
        except Exception:
            skipped += 1
            continue
        if lead.deleted_at is not None:
            skipped += 1
            continue
            
        # Select the telecaller with the lowest current load (Load balancing round-robin)
        valid_pool.sort(key=lambda u: current_loads[u.uid])
        selected_tc = valid_pool[0]
        
        await reassign(engine, lead, selected_tc.uid, by_user_id, reason=AssignmentReason.ROUND_ROBIN.value)
        
        current_loads[selected_tc.uid] += 1
        assigned += 1
        by_tc[selected_tc.uid] = by_tc.get(selected_tc.uid, 0) + 1

    return {"assigned": assigned, "skipped": skipped, "by_telecaller": by_tc}


# --------------------------------------------------------------------------
# Today's callback queue (checkpoint 2.5 — backend support)
# --------------------------------------------------------------------------

async def today_queue(engine, user, *, owner_id: Optional[str] = None,
                      limit: int = 100) -> TodayQueueResponse:
    """The telecaller's callback queue, split into three buckets.

    - **overdue**         — ``follow_up_at`` in the past, oldest first
    - **due_today**       — ``follow_up_at`` later today (IST), soonest first
    - **newly_assigned**  — current active assignment < 24h old, never called

    Role-scoped: a telecaller sees only their own leads; an admin sees all, or a
    single telecaller's via ``owner_id``. Soft-deleted leads are excluded from
    every bucket. Each bucket carries its true ``count`` even when ``items`` is
    capped at ``limit``.
    """
    lead_manager = LeadManager(engine)

    if user.role in (UserRole.SUPER_ADMIN, UserRole.ADMIN):
        scope_owner = owner_id            # None -> all telecallers
    else:
        scope_owner = user.uid            # telecaller -> own leads only

    now = _now()
    now_ist = now.astimezone(IST)
    day_end_ist = (now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
                   + timedelta(days=1))
    cutoff_24h = now - timedelta(hours=24)

    def _scope(q):
        q = q.where(LeadSchema.deleted_at.is_(None))
        if scope_owner:
            q = q.where(LeadSchema.owner_id == scope_owner)
        return q

    user_cache, outlet_cache = {}, {}

    async def _materialize(base, order_col) -> TodayQueueBucket:
        rows_q = base.order_by(order_col).limit(limit)
        count_q = db.select(db.func.count()).select_from(base.subquery())
        async with lead_manager.session_factory() as session:
            total = int((await session.execute(count_q)).scalar_one())
            rows = list((await session.execute(rows_q)).unique().scalars().all())
        items = [
            await build_lead_response(engine, r, user_cache=user_cache,
                                      outlet_cache=outlet_cache)
            for r in rows
        ]
        return TodayQueueBucket(count=total, items=items)

    # Overdue: follow-up time already passed.
    overdue_base = _scope(db.select(LeadSchema)).where(
        LeadSchema.follow_up_at.is_not(None),
        LeadSchema.follow_up_at < now,
    )
    overdue = await _materialize(overdue_base, LeadSchema.follow_up_at.asc())

    # Due today: the remaining part of the IST day (now .. end of today).
    due_base = _scope(db.select(LeadSchema)).where(
        LeadSchema.follow_up_at >= now,
        LeadSchema.follow_up_at < day_end_ist,
    )
    due_today = await _materialize(due_base, LeadSchema.follow_up_at.asc())

    # Newly assigned: a current active assignment < 24h old, with no call yet.
    # Built with IN-subqueries (not a JOIN) so the select stays one-row-per-lead
    # — this keeps the bucket count exact even if a lead momentarily had >1
    # active assignment (e.g. a race during reassignment).
    called_subq = db.select(LeadActivitySchema.lead_id).where(
        LeadActivitySchema.activity_type == LeadActivityType.CALL_LOG
    )
    recent_assign = db.select(LeadAssignmentSchema.lead_id).where(
        LeadAssignmentSchema.is_active.is_(True),
        LeadAssignmentSchema.created_at >= cutoff_24h,
    )
    if scope_owner:
        recent_assign = recent_assign.where(LeadAssignmentSchema.telecaller_id == scope_owner)
    newly_base = _scope(db.select(LeadSchema)).where(
        LeadSchema.uid.in_(recent_assign),
        LeadSchema.uid.not_in(called_subq),
    )
    newly_assigned = await _materialize(newly_base, LeadSchema.created_at.desc())

    return TodayQueueResponse(
        overdue=overdue, due_today=due_today, newly_assigned=newly_assigned,
        generated_at=now,
    )


# --------------------------------------------------------------------------
# Response building (mirrors activity_logs.py enrichment style)
# --------------------------------------------------------------------------

def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def build_lead_response(engine, lead: LeadSchema, *, include_activities: bool = False,
                              user_cache: dict = None, outlet_cache: dict = None) -> LeadResponse:
    user_cache = user_cache if user_cache is not None else {}
    outlet_cache = outlet_cache if outlet_cache is not None else {}

    owner_name = await _resolve_user_name(engine, lead.owner_id, user_cache)
    outlet_name = await _resolve_outlet_name(engine, lead.outlet_id, outlet_cache)

    data = lead.model_dump()
    data["owner_name"] = owner_name
    data["outlet_name"] = outlet_name

    if include_activities:
        # Lead details tab: relabel FB question keys -> English (read-time, no backfill).
        form_id = (lead.campaign_data or {}).get("form_id")
        if form_id and data.get("custom_fields"):
            from services import facebook_mapping
            labels = await facebook_mapping.question_labels(engine, form_id)
            data["custom_fields"] = facebook_mapping.relabel_custom_fields(
                data["custom_fields"], labels)
        activities = await fetch_activities(engine, lead.uid, user_cache=user_cache)
        resp = LeadDetailResponse(**{k: v for k, v in data.items() if k in LeadResponse.model_fields})
        resp.activities = activities
        return resp

    return LeadResponse(**{k: v for k, v in data.items() if k in LeadResponse.model_fields})


async def fetch_activities(engine, lead_id: str, *, limit: int = 100,
                            user_cache: dict = None) -> List[LeadActivityResponse]:
    user_cache = user_cache if user_cache is not None else {}
    activity_manager = LeadActivityManager(engine)
    # Order newest-first explicitly (the base manager's sort flag is inverted).
    async with activity_manager.session_factory() as session:
        query = (
            db.select(LeadActivitySchema)
            .where(LeadActivitySchema.lead_id == lead_id)
            .order_by(LeadActivitySchema.created_at.desc(), LeadActivitySchema.uid.desc())
            .limit(limit)
        )
        rows_items = list((await session.execute(query)).scalars().all())
    out: List[LeadActivityResponse] = []
    for a in rows_items:
        out.append(LeadActivityResponse(
            uid=a.uid, lead_id=a.lead_id, user_id=a.user_id,
            user_name=await _resolve_user_name(engine, a.user_id, user_cache),
            activity_type=a.activity_type.value if hasattr(a.activity_type, "value") else a.activity_type,
            body=a.body, outcome=a.outcome, from_stage=a.from_stage, to_stage=a.to_stage,
            details=a.details, created_at=a.created_at,
        ))
    return out


async def _resolve_user_name(engine, user_id: Optional[str], cache: dict) -> Optional[str]:
    if not user_id:
        return None
    if user_id in cache:
        return cache[user_id]
    try:
        user = await UserManager(engine).fetch(user_id)
        cache[user_id] = user.full_name
    except Exception:
        cache[user_id] = None
    return cache[user_id]


async def _resolve_outlet_name(engine, outlet_id: Optional[str], cache: dict) -> Optional[str]:
    if not outlet_id:
        return None
    if outlet_id in cache:
        return cache[outlet_id]
    try:
        outlet = await OutletManager(engine).fetch(outlet_id)
        cache[outlet_id] = getattr(outlet, "outlet_name", None)
    except Exception:
        cache[outlet_id] = None
    return cache[outlet_id]


if __name__ == "__main__":
    # ponytail: pure-branch self-check for the inbound de-dup pick (DB paths need an engine).
    class _A:                                 # stand-in CALL_LOG row
        def __init__(self, uid, details): self.uid, self.details = uid, details

    web1 = _A("w1", {"direction": "inbound", "call_id": "CS1"})         # webhook auto-log
    web2 = _A("w2", {"direction": "inbound", "call_id": "CS2"})         # newer auto-log
    disp = _A("d1", {"direction": "inbound", "disposition": "Connected"})  # already dispositioned
    out  = _A("o1", {"direction": "outbound", "call_id": "CS3"})        # outbound — ignore

    # newest-first input; exact call_id match wins over recency
    assert _pick_inbound_autolog([web2, web1, out], "CS1") is web1
    # no call_sid -> newest un-dispositioned inbound auto-log
    assert _pick_inbound_autolog([web2, web1], None) is web2
    # dispositioned + outbound rows are never reused
    assert _pick_inbound_autolog([disp, out], "CS9") is None
    assert _pick_inbound_autolog([], "CS1") is None
    print("leadService inbound auto-log pick OK")
