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
import random
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Sequence, Union

import sqlalchemy as db

from managers import (
    UserManager, UserSchema,
    LeadAssignmentManager, LeadAssignmentSchema,
    LeadManager, LeadSchema,
)
from utils.constants import UserRole, TELECALLER_ROLES
from utils.crm_enums import AssignmentReason
from utils.outlet_assignment import auto_assign_outlet
from utils.timeutils import ist_day_start_utc

logger = logging.getLogger(__name__)

# A routed inbound call grants the answering agent temporary rights to see/act on a lead
# they don't own. That grant expires after this window so old cross-team calls stop leaking
# into an agent's list. The row is left in place; it's just ignored past the cutoff.
CALL_ACCESS_TTL = timedelta(hours=8)


def _ttl_cutoff() -> datetime:
    """Grants created before this instant are expired. Tz-AWARE UTC: `created_at` is a
    timestamptz (SharedBackend base schema), so the driver hands back aware datetimes; an
    aware bound also keeps the SQL comparison independent of the session's TimeZone."""
    return datetime.now(timezone.utc) - CALL_ACCESS_TTL


def _grant_live(created_at: datetime, now: datetime) -> bool:
    """Pure TTL predicate (mirrors the SQL cutoff) — a grant is live within CALL_ACCESS_TTL of now."""
    return created_at >= now - CALL_ACCESS_TTL


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


def _as_states(state) -> List[str]:
    """`state` may be one state or several. One ExoPhone can serve a region spanning
    states (Telangana + Andhra Pradesh share a number), so inbound routing passes a list."""
    if not state:
        return []
    if isinstance(state, str):
        return [state]
    return [s for s in state if s]


def _in_states(user_state: Optional[str], states: List[str]) -> bool:
    return any(_same_state(user_state, s) for s in states)


def _restrict(pool: List[UserSchema], only_ids: Optional[set]) -> List[UserSchema]:
    """Keep only telecallers in `only_ids` (e.g. the currently-available set for
    inbound routing). `only_ids=None` means no restriction (the default lead-create
    path). Applied per tier so a tier with no *eligible* member falls through to the
    next region tier rather than dead-ending."""
    return pool if only_ids is None else [u for u in pool if u.uid in only_ids]


async def _active_telecallers(engine, outlet_id: Optional[str],
                              state: Union[str, Sequence[str], None] = None,
                              only_ids: Optional[set] = None,
                              *, allow_cross_state: bool = True) -> List[UserSchema]:
    """Tiered pool: telecallers in the outlet -> same state(s) -> global, optionally
    restricted to `only_ids` (available agents for inbound routing).

    `state` accepts one state or several: inbound routing derives it from the ExoPhone
    the customer dialed, and one number can serve a multi-state region.

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

    # Tier 2: telecallers in the same state(s) (region-scoped fallback).
    states = _as_states(state)
    if states:
        all_active = await user_manager.fetch_all(
            filters={"role": TELECALLER_ROLES, "is_active": True}
        )
        in_state_all = [u for u in all_active.items
                        if _in_states(getattr(u, "state", None), states)]
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


def _ist_day_start_utc() -> datetime:
    """UTC instant of 00:00 IST today, tz-AWARE.

    `leads.created_at` is a timestamptz (SharedBackend base schema), so the driver returns
    aware datetimes. Returning a naive bound here blew up the moment a caller compared it in
    Python rather than in SQL ("can't compare offset-naive and offset-aware datetimes" —
    leadService.distribute_leads._counts), and left the SQL bound leaning on the session's
    TimeZone to be interpreted. Aware is correct for both.

    Delegates to the canonical app.utils.timeutils implementation; kept here
    (and re-exported) since leadService/stateLaneService call it as
    assignmentService._ist_day_start_utc()."""
    return ist_day_start_utc()


async def _received_today_counts(engine, telecaller_ids: List[str]) -> dict:
    """Map telecaller_id -> leads assigned to them since 00:00 IST today (by created_at).

    Both the quota GATE and the fairness BALANCE run off this: an agent is capped once
    they've received assignment_quota leads today (working them doesn't free a slot), and
    among under-cap agents the fewest-given-today wins so everyone logged in gets an equal
    share. Resets at IST midnight. Twin: leadService.distribute_leads._counts."""
    if not telecaller_ids:
        return {}
    cutoff = _ist_day_start_utc()
    mgr = LeadManager(engine)
    async with mgr.session_factory() as session:
        rows = await session.execute(
            db.select(LeadSchema.owner_id, db.func.count())
            .where(
                LeadSchema.owner_id.in_(telecaller_ids),
                LeadSchema.created_at >= cutoff,
                LeadSchema.deleted_at.is_(None),
            )
            .group_by(LeadSchema.owner_id)
        )
        return {oid: int(cnt) for oid, cnt in rows.all()}


def _at_quota(u: UserSchema, today_n: int) -> bool:
    """True if this agent has hit their daily quota (leads received today). 0/None = uncapped."""
    return bool(u.assignment_quota and u.assignment_quota > 0 and today_n >= u.assignment_quota)


def _pick_min(pool: List[UserSchema], counts: dict) -> UserSchema:
    """Give the lead to an agent with the fewest counted leads; RANDOM tie-break so equal
    agents share evenly. A deterministic uid tie-break dumps every tie on the lowest-uid
    agent (the create-path twin of the inbound stickiness that random_pick fixes) — which
    is exactly how one telecaller ended up with 49 leads while 22 got zero."""
    least = min(counts.get(u.uid, 0) for u in pool)
    return random.choice([u for u in pool if counts.get(u.uid, 0) == least])


async def pick_telecaller(engine, outlet_id: Optional[str],
                          state: Union[str, Sequence[str], None] = None,
                          only_ids: Optional[set] = None,
                          *, allow_cross_state: bool = True,
                          enforce_quota: bool = False,
                          random_pick: bool = False) -> Optional[UserSchema]:
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

    `random_pick=True` returns a *random* agent from the pool instead of the
    least-loaded one — used by inbound live-call routing to spread calls evenly
    across whoever's available. min-by-(load, uid) is sticky for inbound: handled
    calls don't add owned-lead load, so ties always break to the same lowest uid
    and one agent gets hammered. Left OFF for the create path, which stays
    load-balanced. Mutually exclusive with enforce_quota (create-only).
    """
    pool = await _active_telecallers(engine, outlet_id, state, only_ids,
                                     allow_cross_state=allow_cross_state)
    if not pool:
        return None
    if random_pick:
        return random.choice(pool)
    if enforce_quota:
        # Quota is a HARD daily cap on leads RECEIVED today (created_at >= 00:00 IST), NOT on
        # the still-untouched backlog: working a lead no longer frees a slot, so assignment
        # stops at exactly assignment_quota and the overflow waits unassigned for a super
        # admin to place. Same metric gates the pool AND balances it (fewest-given-today).
        today = await _received_today_counts(engine, [u.uid for u in pool])
        pool = [u for u in pool if not _at_quota(u, today.get(u.uid, 0))]
        if not pool:
            return None  # everyone in-region at today's quota -> unassigned; sweep/admin places it
        counts = today
    else:
        counts = await _active_assignment_counts(engine, [u.uid for u in pool])
    return _pick_min(pool, counts)


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
    """True if this telecaller has a NON-EXPIRED call-access grant on the lead (see
    grant_call_access + CALL_ACCESS_TTL). Scoped to the call-access reason so a past *owner*
    (reassigned away) does NOT regain rights."""
    mgr = LeadAssignmentManager(engine)
    async with mgr.session_factory() as session:
        row = await session.execute(
            db.select(LeadAssignmentSchema.uid).where(
                LeadAssignmentSchema.lead_id == lead_id,
                LeadAssignmentSchema.telecaller_id == telecaller_id,
                LeadAssignmentSchema.reason == AssignmentReason.INBOUND_CALL_ACCESS.value,
                LeadAssignmentSchema.created_at >= _ttl_cutoff(),
            ).limit(1)
        )
        return row.first() is not None


async def call_access_lead_ids(engine, telecaller_id: str) -> List[str]:
    """Lead ids this telecaller can act on via call-access (not owned), within CALL_ACCESS_TTL.
    Used to widen their lead list so a routed-call lead is findable (e.g. the post-call
    disposition lookup). Grants older than the TTL are dropped so stale cross-team calls
    don't linger in the agent's list."""
    mgr = LeadAssignmentManager(engine)
    async with mgr.session_factory() as session:
        rows = await session.execute(
            db.select(LeadAssignmentSchema.lead_id).where(
                LeadAssignmentSchema.telecaller_id == telecaller_id,
                LeadAssignmentSchema.reason == AssignmentReason.INBOUND_CALL_ACCESS.value,
                LeadAssignmentSchema.created_at >= _ttl_cutoff(),
            )
        )
        return [r[0] for r in rows.all()]


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


if __name__ == "__main__":
    # ponytail: pure TTL-boundary check (DB paths need a live engine).
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _grant_live(now - timedelta(hours=7, minutes=59), now)        # inside 8h -> visible
    assert _grant_live(now, now)                                         # just granted
    assert not _grant_live(now - timedelta(hours=8, minutes=1), now)     # past 8h -> dropped
    print("call-access TTL ok")
