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
)
from utils.constants import UserRole
from utils.crm_enums import AssignmentReason
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
                              only_ids: Optional[set] = None) -> List[UserSchema]:
    """Tiered pool: telecallers in the outlet -> same state -> global, optionally
    restricted to `only_ids` (available agents for inbound routing).

    Region separation: a lead is only assigned across states as a last resort
    (when no telecaller exists in its state at all).
    """
    user_manager = UserManager(engine)

    # Tier 1: telecallers tied to the resolved outlet.
    if outlet_id:
        scoped = await user_manager.fetch_all(
            filters={"role": UserRole.TELECALLER, "is_active": True, "outlet_id": outlet_id}
        )
        eligible = _restrict(list(scoped.items), only_ids)
        if eligible:
            return eligible

    # Tier 2: telecallers in the same state (region-scoped fallback).
    if state:
        all_active = await user_manager.fetch_all(
            filters={"role": UserRole.TELECALLER, "is_active": True}
        )
        in_state = _restrict(
            [u for u in all_active.items if _same_state(getattr(u, "state", None), state)],
            only_ids,
        )
        if in_state:
            return in_state
        # Tier 3 only triggers when the whole state is unstaffed (of eligible agents).
        return _restrict(list(all_active.items), only_ids)

    # No region info at all: global pool.
    everyone = await user_manager.fetch_all(
        filters={"role": UserRole.TELECALLER, "is_active": True}
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


async def pick_telecaller(engine, outlet_id: Optional[str],
                          state: Optional[str] = None,
                          only_ids: Optional[set] = None) -> Optional[UserSchema]:
    """Pick the least-loaded active telecaller for an outlet/state (None if none exist).

    `only_ids` constrains the pool to a given set of telecallers — used by inbound
    routing to pick only among *available* (fresh-heartbeat) agents. Default None
    keeps the original lead-create behaviour (any active telecaller).
    """
    pool = await _active_telecallers(engine, outlet_id, state, only_ids)
    if not pool:
        return None
    counts = await _active_assignment_counts(engine, [u.uid for u in pool])
    # Least active assignments, tie-break deterministically by uid for stable rotation.
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
