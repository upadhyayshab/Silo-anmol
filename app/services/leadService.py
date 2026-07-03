"""Lead service (Feature 1) — Stage 1.

Centralizes every lead mutation so that the activity timeline is written
consistently from one place. Routers stay thin and just translate HTTP.
"""
import asyncio
import logging
import uuid
from bg_tasks import spawn
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List, Tuple

import sqlalchemy as db
from sqlalchemy.exc import IntegrityError

from config import get_engine, get_settings
from managers import (
    LeadManager, LeadSchema,
    LeadActivityManager, LeadActivitySchema,
    UserManager, OutletManager,
)
from models import (
    LeadResponse, LeadDetailResponse, LeadActivityResponse,
    TodayQueueBucket, TodayQueueResponse,
)
from utils.constants import UserRole, TELECALLER_ROLES, OWNER_ROLES
from utils.crm_enums import (LeadStage, LeadActivityType, AssignmentReason,
                             DISPOSITION_OUTCOME, DNC_SUB_DISPOSITIONS,
                             DISPOSITION_STAGE, PROTECTED_STAGES)
from utils import dedup_utils
from services import assignmentService, presenceService

logger = logging.getLogger(__name__)

# India Standard Time — the business is India-wide, so "today" for the working
# queue is bucketed on the IST calendar day.
IST = timezone(timedelta(hours=5, minutes=30))


def _start_of_ist_day(now: datetime) -> datetime:
    """UTC instant of the most recent IST midnight (start of "today" in IST).

    Used to decide whether a lead has been worked *today*: any call logged at or
    after this instant counts. Returned tz-aware in UTC so it compares directly
    against stored timestamps. Pure (no I/O) so it is unit-testable."""
    ist_midnight = now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return ist_midnight.astimezone(timezone.utc)


# Fields a telecaller/admin may edit via PATCH (stage & owner are excluded — they
# have dedicated endpoints that log richer timeline entries).
EDITABLE_FIELDS = {
    "first_name", "last_name", "mobile", "phone", "email",
    "address_line", "address_line_2", "city", "district", "taluk", "state", "pincode", "country",
    "source", "lead_score", "follow_up_at",
    "do_not_call", "do_not_sms", "do_not_email",
    "custom_fields", "campaign_data", "notes",
}

GEO_FIELDS = ("state", "district", "taluk")

# Stages a lead is considered "dormant" in — a re-submitted form on one of these
# reopens the lead (see _reengage). NEW_LEAD/ENGAGED/RTU/FTU are live pipeline
# positions and are left untouched on re-submission.
DORMANT_STAGES = frozenset({LeadStage.LAPSED, LeadStage.NOT_REACHABLE, LeadStage.NOT_QUALIFIED})


def _reengage_target_stage(current):
    """Stage a re-filled dormant lead should reopen to, or None to leave as-is."""
    # normalize current (may be a str value or a LeadStage) to a LeadStage
    cur = current if isinstance(current, LeadStage) else LeadStage(current)
    return LeadStage.ENGAGED if cur in DORMANT_STAGES else None


def canon_geo(value):
    """Lowercase-canonical a geography string so leads line up with the
    outlet_mappings / cluster_districts convention. Blank -> None. Pure
    (no I/O) so it is unit-testable; display layers title-case it back."""
    if not isinstance(value, str):
        return value
    return value.strip().lower() or None


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
        spawn(facebook_capi.send_stage_event(lead, stage))
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
                      *, source_label: Optional[str] = None,
                      reengage_on_merge: bool = True) -> Tuple[LeadSchema, bool]:
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

    # Canonicalize geography to lowercase before any branch (matches
    # outlet_mappings / cluster_districts). Display layers title-case it back.
    for _f in GEO_FIELDS:
        setattr(payload, _f, canon_geo(getattr(payload, _f, None)))

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
        if reengage_on_merge:
            merged = await _reengage(engine, merged, payload, source_label=label, by_user_id=by_user_id)
        return merged, False

    outlet = await assignmentService.resolve_outlet(
        engine, district=payload.district, pincode=payload.pincode, state=payload.state
    )
    outlet_id = outlet.uid if outlet else None
    # Telecaller routing follows the lead's *own* state (where the customer is), falling
    # back to the serving outlet's state only when the lead has none. A warehouse can span
    # states — e.g. AP leads are served by the 'telangana'-tagged AP/Telangana warehouse —
    # and telecallers are tagged by their own state, so using the outlet's state here would
    # aim AP leads at a 'telangana' pool that has zero agents and spill them cross-state.
    routing_state = payload.state or (getattr(outlet, "state", None) if outlet else None)

    if payload.owner_id:
        owner_id = payload.owner_id
        reason = AssignmentReason.MANUAL.value
    elif creator:
        # Attribute the lead to whoever created it.
        owner_id = creator
        reason = AssignmentReason.SELF_CREATED.value
    else:
        # Only auto-assign while at least one telecaller is logged in (CRM/softphone
        # open -> fresh heartbeat). No one present -> picked is None -> the lead is
        # created unassigned and waits for an admin 'distribute' (no auto-drain).
        present = await presenceService.present_ids(engine)
        picked = await assignmentService.pick_telecaller(
            engine, outlet_id, routing_state, only_ids=present,
            allow_cross_state=False,  # no in-region agent -> stays unassigned for manual (super-admin) assignment
            enforce_quota=True,  # cap fresh backlog at assignment_quota; overflow waits for the sweep
        )
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
        taluk=payload.taluk,
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
        if reengage_on_merge:
            merged = await _reengage(engine, merged, payload, source_label=label, by_user_id=by_user_id)
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


async def _reengage(engine, lead, payload, *, source_label, by_user_id):
    """Re-engage a lead that just re-submitted a form (best-effort enrichment).

    Runs after a dedup merge: if the lead is dormant it is reopened to ENGAGED,
    handed to a present telecaller when its owner is gone/inactive, surfaced via
    follow_up_at, logged (RE_ENGAGED), and its owner is notified. The lead is
    ALREADY persisted by the time we get here — every step is best-effort, so any
    failure logs a warning and returns the lead unchanged rather than raising.
    """
    lead_manager = LeadManager(engine)
    try:
        current = lead.stage if isinstance(lead.stage, LeadStage) else LeadStage(lead.stage)
        prev_stage = current
        target = _reengage_target_stage(current)
        reopened = False

        # change_stage does NOT enforce PROTECTED_STAGES (that guard lives in log_call),
        # so it cleanly reopens NOT_QUALIFIED/NOT_REACHABLE/LAPSED -> ENGAGED while logging
        # STAGE_CHANGE and firing CAPI. No manual fallback needed.
        if target is not None:
            lead = await change_stage(engine, lead, LeadStage.ENGAGED, by_user_id=None,
                                      note=f"Re-engaged via {source_label}")
            reopened = True

        # Owner decision: reopen to a present agent if unowned or the owner is inactive.
        reassigned = False
        final_owner = lead.owner_id
        needs_reassign = not lead.owner_id
        if lead.owner_id:
            try:
                owner = await UserManager(engine).fetch(lead.owner_id)
                if not getattr(owner, "is_active", True):
                    needs_reassign = True
            except Exception as e:
                logger.warning(f"[reengage] could not fetch owner {lead.owner_id} for "
                               f"{lead.uid}: {e}")
        if needs_reassign:
            present = await presenceService.present_ids(engine)
            picked = await assignmentService.pick_telecaller(
                engine, lead.outlet_id, lead.state, only_ids=present,
                allow_cross_state=False,  # keep re-engaged leads in-region; else leave for manual assignment
            )
            if picked:
                await reassign(engine, lead, picked.uid, by_user_id="system",
                               reason=AssignmentReason.ROUND_ROBIN.value)
                final_owner = picked.uid
                reassigned = True
                lead = await lead_manager.fetch(lead.uid)

        # Surface it in the telecaller's queue.
        await lead_manager.update(lead.uid, {"follow_up_at": _now()})

        incoming_leadgen = ((payload.campaign_data or {}).get("leadgen_id")
                            if getattr(payload, "campaign_data", None) else None)
        await record_activity(
            engine, lead.uid, LeadActivityType.RE_ENGAGED, user_id=None,
            body=f"Lead re-engaged via {source_label}",
            details={
                "source": source_label,
                "from_stage": prev_stage.value if hasattr(prev_stage, "value") else str(prev_stage),
                "reopened_to": (LeadStage.ENGAGED.value if reopened else None),
                "reassigned": reassigned,
                "owner_id": final_owner,
                "incoming_leadgen_id": incoming_leadgen,
            },
        )

        # Notify the final owner (best-effort; own try/except so a notify failure
        # can't sink the re-engagement).
        if final_owner:
            try:
                # Lazy import: notifications.py builds its own manager off config and
                # imports nothing from services, so this can't cause a circular import.
                from routers.v1.notifications import create_system_notification
                await create_system_notification(
                    user_id=final_owner,
                    title="Lead re-engaged",
                    message=f"{lead.first_name or 'A lead'} ({lead.mobile}) re-submitted a form",
                    notification_type="info",
                )
            except Exception as e:
                logger.warning(f"[reengage] could not notify owner {final_owner} for "
                               f"{lead.uid}: {e}")

        final_lead = await lead_manager.fetch(lead.uid)
        # If we did NOT reopen, no stage change fired CAPI — report the current stage
        # so this touch is still attributed. When reopened, change_stage already fired.
        if not reopened:
            _fire_capi(final_lead, final_lead.stage)
        return final_lead
    except Exception as e:
        logger.warning(f"[reengage] best-effort re-engagement failed for "
                       f"{getattr(lead, 'uid', None)}: {e}")
        return lead


async def update_lead(engine, lead: LeadSchema, changes: Dict[str, Any],
                      by_user_id: str) -> LeadSchema:
    """Apply an editable-field patch and log a single FIELD_UPDATE diff (1.5)."""
    lead_manager = LeadManager(engine)

    # Canonicalize geography before diffing so an unchanged Title-Cased value from
    # the form matches the stored lowercase and produces no spurious update.
    for _f in GEO_FIELDS:
        if _f in changes:
            changes[_f] = canon_geo(changes[_f])

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
    # Auto-advance stage off the disposition (e.g. Not Interested -> Not Qualified, Interested ->
    # Engaged). Converted leads (FTU/RTU) are protected from auto-demotion; "Order Booked" has no
    # entry, so its stage stays driven by real order placement.
    if sub_disposition:
        target = DISPOSITION_STAGE.get(sub_disposition)
        current = lead.stage.value if hasattr(lead.stage, "value") else lead.stage
        if target and current not in PROTECTED_STAGES and current != target.value:
            await change_stage(engine, lead, target, by_user_id=by_user_id,
                               note=f"Auto: {sub_disposition} → {target.value}")
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
    # An explicit target pool (admin picked telecallers) is honored as-is — a super admin can
    # place any lead on any telecaller. An automatic distribution (sweep / no pool given) stays
    # strictly within each lead's own state; a lead with no in-region agent is left unassigned
    # (never spilled to the global pool) for a super admin to assign manually.
    auto = not telecaller_ids

    async def get_active_count(uid: str) -> int:
        open_leads = await lead_manager.fetch_all(filters={"owner_id": uid})
        # Quota currency = UNTOUCHED leads (stage still New Lead). Working a lead frees
        # its slot (any disposition moves the stage off New Lead), so the sweep tops an
        # agent back up as they clear their fresh backlog. Twin of
        # assignmentService._fresh_counts — keep the two in sync.
        return sum(
            1 for l in open_leads.items
            if l.deleted_at is None
            and (l.stage.value if hasattr(l.stage, "value") else l.stage) == LeadStage.NEW_LEAD.value
        )

    async def add_to_pool(u):
        # Auto/sweep round-robin is telecallers only; an explicit admin pick (Change
        # Owner popup, auto=False) may also land on an agency admin — they can own leads.
        if u.role not in (OWNER_ROLES if not auto else TELECALLER_ROLES) or not u.is_active:
            return
        active_count = await get_active_count(u.uid)
        # Explicit admin pick (Change Owner / chosen pool, auto=False): honor it regardless
        # of online status or quota — the super admin deliberately chose this telecaller.
        if not auto:
            pool_objects.append(u)
            current_loads[u.uid] = active_count
            return
        # Auto/sweep only: skip offline telecallers (no heartbeat > 30m) and those at quota.
        if not u.last_active_at or (now - u.last_active_at.replace(tzinfo=timezone.utc)).total_seconds() > 1800:
            return
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
            filters={"role": TELECALLER_ROLES, "is_active": True}
        )
        for u in active.items:
            await add_to_pool(u)

    if not pool_objects:
        return {"assigned": 0, "skipped": len(lead_ids or []), "by_telecaller": {},
                "detail": "no active/online telecallers available or all quotas full"}

    assigned, skipped, by_tc = 0, 0, {}
    
    for lid in lead_ids or []:
        try:
            lead = await lead_manager.fetch(lid)
        except Exception:
            skipped += 1
            continue
        if lead.deleted_at is not None:
            skipped += 1
            continue

        # Re-evaluate pool to exclude those who just hit their quota (auto/sweep only;
        # an explicit admin pick is honored regardless of quota).
        if auto:
            valid_pool = [u for u in pool_objects if not (u.assignment_quota and u.assignment_quota > 0 and current_loads[u.uid] >= u.assignment_quota)]
        else:
            valid_pool = list(pool_objects)

        # Region separation (auto/sweep only): keep a lead strictly within its own state.
        # No in-state agent (unstaffed, all offline/over-quota, or the lead has no state) ->
        # skip it, leaving it unassigned for a super admin to place manually rather than
        # spilling across state lines. Mirrors assignmentService._active_telecallers.
        lead_state = getattr(lead, "state", None)
        if auto:
            in_state = [u for u in valid_pool
                        if lead_state and assignmentService._same_state(getattr(u, "state", None), lead_state)]
            if not in_state:
                skipped += 1
                continue
            valid_pool = in_state

        if not valid_pool:
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


async def sweep_unassigned():
    """Scheduled sweep: hand any leads sitting unassigned (e.g. created overnight while
    everyone was offline) to online telecallers. Reuses distribute_leads, so the
    <30m-online filter, assignment_quota cap and least-loaded balancing all apply —
    leads with no online/under-quota agent simply stay unassigned for the next sweep.
    ponytail: distribute_leads runs O(agents) count-queries; fine at this scale."""
    engine = get_engine(get_settings().name)
    unassigned = await LeadManager(engine).fetch_all(filters={"owner_id": None})
    lead_ids = [l.uid for l in unassigned.items if l.deleted_at is None]
    if not lead_ids:
        return {"assigned": 0, "skipped": 0}
    result = await distribute_leads(engine, lead_ids, None, by_user_id="system")
    logger.info("sweep_unassigned: distributed %s/%s unassigned leads",
                result.get("assigned"), len(lead_ids))
    return result


# --------------------------------------------------------------------------
# Today's callback queue (checkpoint 2.5 — backend support)
# --------------------------------------------------------------------------

async def today_queue(engine, user, *, owner_id: Optional[str] = None,
                      limit: int = 100) -> TodayQueueResponse:
    """The telecaller's working queue, split into three stage buckets.

    - **new**            — stage New Lead, newest first
    - **engaged**        — stage Engaged, earliest follow-up first
    - **not_reachable**  — stage Not Reachable, oldest-touched first

    A lead drops out of every bucket once a call is logged *today* (IST calendar
    day), and reappears the next day if it is still in one of these stages — so
    working a lead clears it for the day, while unreached leads roll to tomorrow.
    Logging a call also auto-advances the stage via the disposition map, so a
    connected lead naturally leaves "new"/"not_reachable" on its own.

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
    today_start = _start_of_ist_day(now)

    # Leads already worked today: a call logged at/after IST midnight. An
    # IN-subquery (not a JOIN) keeps the select one-row-per-lead so bucket counts
    # stay exact even when a lead has several call logs.
    worked_today = db.select(LeadActivitySchema.lead_id).where(
        LeadActivitySchema.activity_type == LeadActivityType.CALL_LOG,
        LeadActivitySchema.created_at >= today_start,
    )

    def _bucket_base(stage):
        q = db.select(LeadSchema).where(
            LeadSchema.deleted_at.is_(None),
            LeadSchema.stage == stage.value,
            LeadSchema.uid.not_in(worked_today),
        )
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

    # Postgres orders NULLs last on ASC, so engaged leads without a scheduled
    # follow-up naturally sort after those with one.
    new = await _materialize(_bucket_base(LeadStage.NEW_LEAD), LeadSchema.created_at.desc())
    engaged = await _materialize(_bucket_base(LeadStage.ENGAGED), LeadSchema.follow_up_at.asc())
    not_reachable = await _materialize(_bucket_base(LeadStage.NOT_REACHABLE), LeadSchema.updated_at.asc())

    return TodayQueueResponse(
        new=new, engaged=engaged, not_reachable=not_reachable,
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


async def latest_dispositions(engine, lead_ids: List[str]) -> Dict[str, Dict[str, Optional[str]]]:
    """Map lead_id -> {disposition, sub_disposition} from each lead's most recent CALL_LOG.
    One batched query (avoids N+1 across a list page). The disposition label mirrors the
    superadmin report so both surfaces agree."""
    if not lead_ids:
        return {}
    from services import crmReportService
    out: Dict[str, Dict[str, Optional[str]]] = {}
    async with LeadActivityManager(engine).session_factory() as session:
        rows = (await session.execute(
            db.select(LeadActivitySchema)
              .where(LeadActivitySchema.lead_id.in_(lead_ids),
                     LeadActivitySchema.activity_type == LeadActivityType.CALL_LOG)
              .order_by(LeadActivitySchema.created_at.desc())
        )).scalars().all()
    for a in rows:
        if a.lead_id in out:
            continue  # newest-first -> first seen is the latest call
        details = a.details or {}
        out[a.lead_id] = {
            "disposition": crmReportService._disposition_label(details, a.outcome) or None,
            "sub_disposition": details.get("sub_disposition") or None,
        }
    return out


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
