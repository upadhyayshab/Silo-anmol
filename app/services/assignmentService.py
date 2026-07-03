"""Lead assignment engine (Feature 4) — Stage 1: outlet-scoped round-robin.

New leads are auto-assigned to the least-loaded active telecaller within the
lead's resolved outlet (so the team that serves that region works the lead),
falling back to all active telecallers if that outlet has none.

"Least-loaded" (fewest currently-active assignments, deterministic tie-break)
produces the same even distribution as a classic round-robin for back-to-back
creation — e.g. 10 leads across 3 telecallers -> 4/3/3 — without needing a
separate cursor table.
"""
import logging
from typing import Optional, List

import sqlalchemy as db

from managers import (
    UserManager, UserSchema,
    LeadAssignmentManager, LeadAssignmentSchema,
    LeadManager, LeadSchema,
)
from utils.constants import UserRole, TELECALLER_ROLES
from utils.crm_enums import AssignmentReason, LeadStage
from utils.outlet_assignment import auto_assign_outlet

logger = logging.getLogger(__name__)


async def resolve_outlet(engine, *, district: Optional[str] = None,
                         pincode: Optional[str] = None,
                         state: Optional[str] = None):
    """Resolve the serving outlet for a lead's location (reuses ERP outlet logic).

    Returns the outlet object (its `.uid` and `.state` are used for routing) or None.
    """
    try:
        return await auto_assign_outlet(engine, district=district, pincode=pincode, state=state)
    except Exception as e:  # outlet resolution must never block lead creation
        logger.warning(f"[assignment] outlet resolution failed: {e}")
        return None


def _same_state(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


def _restrict(pool: List[UserSchema], only_ids: Optional[set]) -> List[UserSchema]:
    """Keep only telecallers in `only_ids` (e.g. the currently-available set for
    inbound routing). `only_ids=None` means no restriction (the default lead-create
    path). Applied per tier so a tier with no *eligible* member falls through to the
    next region tier rather than dead-ending."""
    return pool if only_ids is None else [u for u in pool if u.uid in only_ids]


async def _active_telecallers(engine, outlet_id: Optional[str],
                              state: Optional[str],
                              only_ids: Optional[set] = None,
                              *, allow_cross_state: bool = True) -> List[UserSchema]:
    """Tiered pool: telecallers in the outlet -> same state -> global, optionally
    restricted to `only_ids` (available agents for inbound routing).

    Region separation: a lead is only assigned across states as a last resort
    (when no telecaller exists in its state at all).

    `allow_cross_state=False` disables that last resort: an unstaffed/no-region
    lead returns [] (stays unassigned for a super admin to place manually) instead
    of spilling to the global pool. Auto lead-assignment passes False; inbound call
    routing keeps the default True (a live call must still ring an available agent).
    """
    user_manager = UserManager(engine)

    # Tier 1: telecallers tied to the resolved outlet.
    if outlet_id:
        scoped = await user_manager.fetch_all(
            filters={"role": TELECALLER_ROLES, "is_active": True, "outlet_id": outlet_id}
        )
        eligible = _restrict(list(scoped.items), only_ids)
        if eligible:
            return eligible

    # Tier 2: telecallers in the same state (region-scoped fallback).
    if state:
        all_active = await user_manager.fetch_all(
            filters={"role": TELECALLER_ROLES, "is_active": True}
        )
        in_state_all = [u for u in all_active.items if _same_state(getattr(u, "state", None), state)]
        in_state = _restrict(in_state_all, only_ids)
        if in_state:
            return in_state
        # In-state telecallers exist but none are eligible right now (offline / not in the
        # available set): leave the lead unassigned for the next sweep rather than spill it
        # across state lines. Assigning across states silently is the actual bug — an AP
        # lead handed to a Karnataka agent because AP agents happened to be offline.
        if in_state_all:
            return []
        # Tier 3: the state is genuinely unstaffed (no active telecaller at all). Cross state
        # lines only for callers that opt in (inbound routing); auto-assign leaves it unassigned.
        if not allow_cross_state:
            return []
        return _restrict(list(all_active.items), only_ids)

    # No region info at all: global pool (auto-assign opts out -> leave unassigned).
    if not allow_cross_state:
        return []
    everyone = await user_manager.fetch_all(
        filters={"role": TELECALLER_ROLES, "is_active": True}
    )
    return _restrict(list(everyone.items), only_ids)


async def _active_assignment_counts(engine, telecaller_ids: List[str]) -> dict:
    """Map telecaller_id -> count of currently-active lead assignments."""
    if not telecaller_ids:
        return {}
    mgr = LeadAssignmentManager(engine)
    async with mgr.session_factory() as session:
        query = (
            db.select(LeadAssignmentSchema.telecaller_id, db.func.count())
            .where(
                LeadAssignmentSchema.telecaller_id.in_(telecaller_ids),
                LeadAssignmentSchema.is_active.is_(True),
            )
            .group_by(LeadAssignmentSchema.telecaller_id)
        )
        rows = await session.execute(query)
        return {tid: int(cnt) for tid, cnt in rows.all()}


async def _fresh_counts(engine, telecaller_ids: List[str]) -> dict:
    """Map telecaller_id -> count of UNTOUCHED leads they own (stage still New Lead).

    This is the assignment_quota currency: a lead frees its slot the moment it's
    worked (any disposition moves the stage off New Lead), so the quota caps how many
    un-started leads an agent may hold, not their total open book. Twin of
    leadService.distribute_leads.get_active_count — keep the two definitions in sync."""
    if not telecaller_ids:
        return {}
    mgr = LeadManager(engine)
    async with mgr.session_factory() as session:
        rows = await session.execute(
            db.select(LeadSchema.owner_id, db.func.count())
            .where(
                LeadSchema.owner_id.in_(telecaller_ids),
                LeadSchema.stage == LeadStage.NEW_LEAD.value,
                LeadSchema.deleted_at.is_(None),
            )
            .group_by(LeadSchema.owner_id)
        )
        return {oid: int(cnt) for oid, cnt in rows.all()}


def _at_quota(u: UserSchema, fresh_n: int) -> bool:
    """True if this agent is at/over their fresh-lead quota. quota 0/None = uncapped."""
    return bool(u.assignment_quota and u.assignment_quota > 0 and fresh_n >= u.assignment_quota)


async def pick_telecaller(engine, outlet_id: Optional[str],
                          state: Optional[str] = None,
                          only_ids: Optional[set] = None,
                          *, allow_cross_state: bool = True,
                          enforce_quota: bool = False) -> Optional[UserSchema]:
    """Pick the least-loaded active telecaller for an outlet/state (None if none exist).

    `only_ids` constrains the pool to a given set of telecallers — used by inbound
    routing to pick only among *available* (fresh-heartbeat) agents. Default None
    keeps the original lead-create behaviour (any active telecaller).

    `allow_cross_state=False` keeps auto-assignment in-region: no in-state agent ->
    None (unassigned, for a super admin to place manually), never a cross-state pick.

    `enforce_quota=True` drops agents already at their fresh-lead quota (untouched
    New-Lead count >= assignment_quota) and balances by that same fresh count — so
    the create path caps a fresh backlog and lets overflow wait unassigned. Left OFF
    by default: inbound live-call routing must still ring an at-quota agent.
    """
    pool = await _active_telecallers(engine, outlet_id, state, only_ids,
                                     allow_cross_state=allow_cross_state)
    if not pool:
        return None
    if enforce_quota:
        counts = await _fresh_counts(engine, [u.uid for u in pool])
        pool = [u for u in pool if not _at_quota(u, counts.get(u.uid, 0))]
        if not pool:
            return None  # everyone in-region at quota -> unassigned; the 5-min sweep retries
    else:
        counts = await _active_assignment_counts(engine, [u.uid for u in pool])
    # Least loaded, tie-break deterministically by uid for stable rotation.
    return min(pool, key=lambda u: (counts.get(u.uid, 0), u.uid))


async def grant_call_access(engine, lead_id: str, telecaller_id: str) -> None:
    """Let a telecaller who handled a routed inbound call act on a lead they don't own
    (disposition, notes, orders, ...) WITHOUT changing ownership. Idempotent.

    ponytail: reuses lead_assignments instead of a new table — is_active=False marks it a
    non-owning grant, so _active_assignment_counts (filters is_active=True) ignores it and
    round-robin load is unaffected. Promote to a dedicated table only if call-access ever
    needs its own lifecycle (expiry/revoke)."""
    mgr = LeadAssignmentManager(engine)
    existing = await mgr.fetch_all(filters={
        "lead_id": lead_id, "telecaller_id": telecaller_id,
        "reason": AssignmentReason.INBOUND_CALL_ACCESS.value})
    if existing.items:
        return
    await mgr.create(LeadAssignmentSchema(
        lead_id=lead_id, telecaller_id=telecaller_id,
        reason=AssignmentReason.INBOUND_CALL_ACCESS.value,
        assigned_by="system", is_active=False))


async def has_call_access(engine, lead_id: str, telecaller_id: str) -> bool:
    """True if this telecaller was granted call-access on the lead (see grant_call_access).
    Scoped to the call-access reason so a past *owner* (reassigned away) does NOT regain rights."""
    mgr = LeadAssignmentManager(engine)
    rows = await mgr.fetch_all(filters={
        "lead_id": lead_id, "telecaller_id": telecaller_id,
        "reason": AssignmentReason.INBOUND_CALL_ACCESS.value})
    return bool(rows.items)


async def call_access_lead_ids(engine, telecaller_id: str) -> List[str]:
    """Lead ids this telecaller can act on via call-access (not owned). Used to widen their
    lead list so a routed-call lead is findable (e.g. the post-call disposition lookup).
    ponytail: returns all of them; fine for realistic per-agent volumes (hundreds)."""
    mgr = LeadAssignmentManager(engine)
    rows = await mgr.fetch_all(filters={
        "telecaller_id": telecaller_id,
        "reason": AssignmentReason.INBOUND_CALL_ACCESS.value})
    return [r.lead_id for r in rows.items]


async def record_assignment(engine, lead_id: str, telecaller_id: str, *,
                            reason: str = AssignmentReason.ROUND_ROBIN.value,
                            assigned_by: str = "system") -> LeadAssignmentSchema:
    """Persist an assignment, deactivating any previous active one for the lead."""
    mgr = LeadAssignmentManager(engine)
    # Deactivate prior active assignment(s) for this lead.
    existing = await mgr.fetch_all(filters={"lead_id": lead_id, "is_active": True})
    for row in existing.items:
        await mgr.update(row.uid, {"is_active": False})
    return await mgr.create(LeadAssignmentSchema(
        lead_id=lead_id,
        telecaller_id=telecaller_id,
        reason=reason,
        assigned_by=assigned_by,
        is_active=True,
    ))
