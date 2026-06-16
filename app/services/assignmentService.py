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


async def _active_telecallers(engine, outlet_id: Optional[str],
                              state: Optional[str]) -> List[UserSchema]:
    """Tiered pool: telecallers in the outlet -> same state -> global.

    Region separation: a lead is only assigned across states as a last resort
    (when no telecaller exists in its state at all).
    """
    user_manager = UserManager(engine)

    # Tier 1: telecallers tied to the resolved outlet.
    if outlet_id:
        scoped = await user_manager.fetch_all(
            filters={"role": UserRole.TELECALLER, "is_active": True, "outlet_id": outlet_id}
        )
        if scoped.items:
            return list(scoped.items)

    # Tier 2: telecallers in the same state (region-scoped fallback).
    if state:
        all_active = await user_manager.fetch_all(
            filters={"role": UserRole.TELECALLER, "is_active": True}
        )
        in_state = [u for u in all_active.items if _same_state(getattr(u, "state", None), state)]
        if in_state:
            return in_state
        # Tier 3 only triggers when the whole state is unstaffed.
        return list(all_active.items)

    # No region info at all: global pool.
    everyone = await user_manager.fetch_all(
        filters={"role": UserRole.TELECALLER, "is_active": True}
    )
    return list(everyone.items)


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
                          state: Optional[str] = None) -> Optional[UserSchema]:
    """Pick the least-loaded active telecaller for an outlet/state (None if none exist)."""
    pool = await _active_telecallers(engine, outlet_id, state)
    if not pool:
        return None
    counts = await _active_assignment_counts(engine, [u.uid for u in pool])
    # Least active assignments, tie-break deterministically by uid for stable rotation.
    return min(pool, key=lambda u: (counts.get(u.uid, 0), u.uid))


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
