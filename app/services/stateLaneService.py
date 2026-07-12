"""State-Lane dashboard aggregation (CRM admin monitor).

The rollup math (`assemble_lanes`) is a pure function so it is unit-tested
DB-free, matching the rest of the CRM suite; `state_lane_overview` is the thin
DB-read layer that feeds it. `distribute_state_backlog` is wiring over the
existing least-loaded distributor (mirrors leadService.sweep_unassigned).
"""
from datetime import datetime, timezone, date
from typing import Optional, List, Dict, Any

import sqlalchemy as db

from managers import LeadManager, LeadSchema, UserSchema, FacebookPageSchema
from services import leadService
from utils.constants import TELECALLER_ROLES

# "online" = seen within this window (matches assignmentService auto-assign).
ONLINE_WINDOW_SECONDS = 1800

UNROUTED = "Unrouted"        # synthetic lane: pages with no routing_state
OTHER = "Other / unmapped"   # synthetic lane: leads whose state maps to nothing


def _online(last_active_at: Optional[datetime], now: datetime) -> bool:
    if not last_active_at:
        return False
    la = last_active_at if last_active_at.tzinfo else last_active_at.replace(tzinfo=timezone.utc)
    return (now - la).total_seconds() <= ONLINE_WINDOW_SECONDS


def _fill_pct(load: int, quota: int) -> int:
    return round(load / quota * 100) if quota else 0


def assemble_lanes(*, pages: List[dict], leads_in_by_state: Dict, leads_in_by_page: Dict,
                   unassigned_by_state: Dict, load_by_owner: Dict,
                   telecallers: List[dict], now: datetime) -> Dict[str, Any]:
    """Fold the primitive rollups into per-state lanes + totals. Pure/DB-free."""
    # Real lanes = every non-null state seen across pages, telecallers, and leads.
    real_states = set()
    real_states |= {p["routing_state"] for p in pages if p.get("routing_state")}
    real_states |= {t["state"] for t in telecallers if t.get("state")}
    real_states |= {s for s in leads_in_by_state if s}
    real_states |= {s for s in unassigned_by_state if s}

    def _tc(t):
        load = load_by_owner.get(t["uid"], 0)
        quota = t.get("quota") or 0
        return {"uid": t["uid"], "name": t["name"], "load": load, "quota": quota,
                "fill_pct": _fill_pct(load, quota), "online": _online(t.get("last_active_at"), now),
                "last_active_at": t.get("last_active_at")}

    def _pages_for(state):
        out = []
        for p in pages:
            if (p.get("routing_state") or UNROUTED) == state:
                out.append({"page_id": p["page_id"], "page_name": p.get("page_name"),
                            "routing_state": p.get("routing_state"),
                            "leads_in": leads_in_by_page.get(p["page_id"], 0)})
        return out

    lanes = []
    for state in sorted(real_states):
        tcs = [_tc(t) for t in telecallers if t.get("state") == state]
        lanes.append({
            "state": state, "is_mapped": True,
            "pages": _pages_for(state),
            "leads_in": leads_in_by_state.get(state, 0),
            "unassigned": unassigned_by_state.get(state, 0),
            "capacity": sum(t["quota"] for t in tcs),
            "load": sum(t["load"] for t in tcs),
            "telecallers": tcs,
        })

    # UNROUTED: pages with no routing_state (surfaced so an admin can fix them).
    unrouted_pages = _pages_for(UNROUTED)
    if unrouted_pages:
        lanes.append({"state": UNROUTED, "is_mapped": False, "pages": unrouted_pages,
                      "leads_in": sum(p["leads_in"] for p in unrouted_pages),
                      "unassigned": 0, "capacity": 0, "load": 0, "telecallers": []})

    # OTHER: leads whose state is null / maps to nothing, + any stateless telecaller.
    other_tcs = [_tc(t) for t in telecallers if not t.get("state")]
    other_leads_in = leads_in_by_state.get(None, 0)
    other_unassigned = unassigned_by_state.get(None, 0)
    if other_leads_in or other_unassigned or other_tcs:
        lanes.append({"state": OTHER, "is_mapped": False, "pages": [],
                      "leads_in": other_leads_in, "unassigned": other_unassigned,
                      "capacity": sum(t["quota"] for t in other_tcs),
                      "load": sum(t["load"] for t in other_tcs), "telecallers": other_tcs})

    totals = {
        "leads_in": sum(leads_in_by_state.values()),
        "unassigned": sum(unassigned_by_state.values()),
        "capacity": sum((t.get("quota") or 0) for t in telecallers),
        "load": sum(load_by_owner.values()),
    }
    return {"totals": totals, "lanes": lanes}


def _day_bounds(from_date: Optional[date], to_date: Optional[date]):
    """Inclusive UTC day bounds on lead.created_at. Neither given -> today."""
    if from_date is None and to_date is None:
        today = datetime.now(timezone.utc).date()
        from_date = to_date = today
    gte = datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc) if from_date else None
    lte = datetime.combine(to_date, datetime.max.time(), tzinfo=timezone.utc) if to_date else None
    return gte, lte


def _role_value(role):
    return role.value if hasattr(role, "value") else role


async def state_lane_overview(engine, *, from_date: Optional[date] = None,
                              to_date: Optional[date] = None) -> Dict[str, Any]:
    """Batched GROUP BY reads -> assemble_lanes. The date range (default: today) scopes
    both 'leads_in' and each telecaller's 'load' (leads assigned in-window); backlog
    (unassigned), quota and online are always live."""
    gte, lte = _day_bounds(from_date, to_date)
    telecaller_role_vals = {_role_value(r) for r in TELECALLER_ROLES}
    lm = LeadManager(engine)
    async with lm.session_factory() as session:
        pages = [{"page_id": pid, "page_name": name, "routing_state": rs} for pid, name, rs in (
            await session.execute(db.select(
                FacebookPageSchema.page_id, FacebookPageSchema.page_name,
                FacebookPageSchema.routing_state))).all()]

        range_conds = [LeadSchema.deleted_at.is_(None)]
        if gte is not None:
            range_conds.append(LeadSchema.created_at >= gte)
        if lte is not None:
            range_conds.append(LeadSchema.created_at <= lte)

        leads_in_by_state = {s: int(c) for s, c in (await session.execute(
            db.select(LeadSchema.state, db.func.count()).where(*range_conds)
              .group_by(LeadSchema.state))).all()}

        page_key = LeadSchema.campaign_data["page_id"].as_string()
        leads_in_by_page = {p: int(c) for p, c in (await session.execute(
            db.select(page_key, db.func.count()).where(*range_conds, page_key.isnot(None))
              .group_by(page_key))).all()}

        unassigned_by_state = {s: int(c) for s, c in (await session.execute(
            db.select(LeadSchema.state, db.func.count()).where(
                LeadSchema.deleted_at.is_(None), LeadSchema.owner_id.is_(None))
              .group_by(LeadSchema.state))).all()}

        # Load = leads ASSIGNED to the owner in this window (owner set), using the SAME
        # created_at range as leads_in above. Defaults to today (see _day_bounds), so the
        # bar reads "assigned today / quota" — the metric an admin uses to see who the day's
        # leads went to (whose share ran high). It pairs with the lane's "N in": of the N
        # leads that came in this window, each telecaller got `load` of them.
        # (Previously counted only fresh New-Lead backlog — pendency, which hid the day's
        # distribution; a fast worker who cleared their leads showed 0 despite a heavy day.)
        load_by_owner = {o: int(c) for o, c in (await session.execute(
            db.select(LeadSchema.owner_id, db.func.count()).where(
                *range_conds, LeadSchema.owner_id.isnot(None))
              .group_by(LeadSchema.owner_id))).all()}

        telecallers = [
            {"uid": uid, "name": name or uid, "state": st, "quota": quota or 0, "last_active_at": la}
            for uid, name, st, quota, la, role in (await session.execute(db.select(
                UserSchema.uid, UserSchema.full_name, UserSchema.state,
                UserSchema.assignment_quota, UserSchema.last_active_at, UserSchema.role)
                .where(UserSchema.is_active.is_(True)))).all()
            if _role_value(role) in telecaller_role_vals]

    return assemble_lanes(
        pages=pages, leads_in_by_state=leads_in_by_state, leads_in_by_page=leads_in_by_page,
        unassigned_by_state=unassigned_by_state, load_by_owner=load_by_owner,
        telecallers=telecallers, now=datetime.now(timezone.utc))


async def distribute_state_backlog(engine, state: str, by_user_id: str) -> Dict[str, Any]:
    """Hand this state's live unassigned leads to the least-loaded distributor.
    Mirrors sweep_unassigned: fetch the backlog, drop soft-deleted, delegate the
    online/quota/least-loaded logic to distribute_leads (tested there)."""
    res = await LeadManager(engine).fetch_all(filters={"owner_id": None, "state": state})
    lead_ids = [l.uid for l in res.items if l.deleted_at is None]
    if not lead_ids:
        return {"assigned": 0, "skipped": 0, "by_telecaller": {},
                "detail": f"no unassigned leads in {state}"}
    return await leadService.distribute_leads(engine, lead_ids, None, by_user_id=by_user_id)
