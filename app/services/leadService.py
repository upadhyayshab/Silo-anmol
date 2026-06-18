"""Lead service (Feature 1) — Stage 1.

Centralizes every lead mutation so that the activity timeline is written
consistently from one place. Routers stay thin and just translate HTTP.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import sqlalchemy as db

from managers import (
    LeadManager, LeadSchema,
    LeadActivityManager, LeadActivitySchema,
    UserManager, OutletManager,
)
from models import LeadResponse, LeadDetailResponse, LeadActivityResponse
from utils.constants import UserRole
from utils.crm_enums import LeadStage, LeadActivityType, AssignmentReason
from services import assignmentService

logger = logging.getLogger(__name__)


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

async def create_lead(engine, payload, by_user_id: str) -> LeadSchema:
    """Create a lead and assign an owner.

    Ownership rules:
      1. explicit ``payload.owner_id``  -> assigned to that telecaller (manual)
      2. created by a real user (telecaller/admin via API) -> attributed to the creator
      3. system / webhook (no human creator) -> round-robin within the lead's region

    Admins redistribute later via ``distribute_leads`` / the assign endpoint.
    """
    lead_manager = LeadManager(engine)

    creator = _real_user(by_user_id)  # None for system/webhook/microservice

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
        mobile=payload.mobile,
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
        custom_fields=payload.custom_fields,
        campaign_data=payload.campaign_data,
        notes=payload.notes,
        lead_number=_gen_lead_number(),
        stage=LeadStage.NEW_LEAD,
        owner_id=owner_id,
        outlet_id=outlet_id,
        last_activity_at=_now(),
    )
    lead = await lead_manager.create(lead)

    await record_activity(engine, lead.uid, LeadActivityType.CREATED, user_id=creator,
                          body=f"Lead {lead.lead_number} created")

    if owner_id:
        await assignmentService.record_assignment(
            engine, lead.uid, owner_id, reason=reason, assigned_by=(creator or "system"),
        )
        await record_activity(engine, lead.uid, LeadActivityType.ASSIGNMENT,
                              user_id=creator, body=f"Assigned to telecaller ({reason})",
                              details={"telecaller_id": owner_id, "reason": reason})

    return await lead_manager.fetch(lead.uid)


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
                       by_user_id: str, note: Optional[str] = None) -> LeadSchema:
    lead_manager = LeadManager(engine)
    from_stage = lead.stage.value if hasattr(lead.stage, "value") else lead.stage
    to_stage = new_stage.value if hasattr(new_stage, "value") else new_stage

    updated = await lead_manager.update(lead.uid, {"stage": new_stage})
    await record_activity(engine, lead.uid, LeadActivityType.STAGE_CHANGE,
                          user_id=by_user_id, from_stage=from_stage, to_stage=to_stage,
                          body=note or f"Stage changed {from_stage} → {to_stage}")
    return updated


async def add_note(engine, lead_id: str, body: str, by_user_id: str) -> LeadActivitySchema:
    return await record_activity(engine, lead_id, LeadActivityType.NOTE,
                                 user_id=by_user_id, body=body)


async def log_call(engine, lead: LeadSchema, outcome: str, note: Optional[str],
                   follow_up_at: Optional[datetime], by_user_id: str) -> LeadActivitySchema:
    if follow_up_at is not None:
        await LeadManager(engine).update(lead.uid, {"follow_up_at": follow_up_at})
    return await record_activity(engine, lead.uid, LeadActivityType.CALL_LOG,
                                 user_id=by_user_id, outcome=outcome, body=note)


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

    # Build the validated target pool (active telecallers only).
    if telecaller_ids:
        pool: List[str] = []
        for tid in telecaller_ids:
            try:
                u = await user_manager.fetch(tid)
            except Exception:
                continue
            if u.role == UserRole.TELECALLER and u.is_active:
                pool.append(u.uid)
    else:
        active = await user_manager.fetch_all(
            filters={"role": UserRole.TELECALLER, "is_active": True}
        )
        pool = [u.uid for u in active.items]

    if not pool:
        return {"assigned": 0, "skipped": len(lead_ids or []), "by_telecaller": {},
                "detail": "no active telecallers in the target pool"}

    assigned, skipped, by_tc, i = 0, 0, {}, 0
    for lid in lead_ids or []:
        try:
            lead = await lead_manager.fetch(lid)
        except Exception:
            skipped += 1
            continue
        if lead.deleted_at is not None:
            skipped += 1
            continue
        tid = pool[i % len(pool)]   # even round-robin across the batch
        i += 1
        await reassign(engine, lead, tid, by_user_id, reason=AssignmentReason.ROUND_ROBIN.value)
        assigned += 1
        by_tc[tid] = by_tc.get(tid, 0) + 1

    return {"assigned": assigned, "skipped": skipped, "by_telecaller": by_tc}


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
