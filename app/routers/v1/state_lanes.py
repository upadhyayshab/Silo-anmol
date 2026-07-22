"""CRM State-Lane dashboard — per-state FB-lead flow into telecaller queues.

Read-only overview + a per-state 'distribute the backlog' action. Both admin-only
(LEADS_MANAGE). All rollup logic lives in services.stateLaneService.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, Path
from pydantic import BaseModel, Field

from config import get_settings, get_engine
from services import stateLaneService
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission


class StateQuotaRequest(BaseModel):
    assignment_quota: int = Field(..., ge=0, le=100000)

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter(prefix="/crm/state-lanes", tags=["CRM - State Lanes"])


@router.get("")
async def get_state_lanes(
    from_date: Optional[date] = Query(None, description="Lead created-at lower bound (default: today)"),
    to_date: Optional[date] = Query(None, description="Lead created-at upper bound (default: today)"),
    _: AuthContext = Depends(require_permission(Permission.LEADS_MANAGE)),
):
    """Per-state lanes: feeding FB pages, incoming volume (date range), live
    unassigned backlog, and each telecaller's load vs quota. See design doc for shape."""
    return await stateLaneService.state_lane_overview(engine, from_date=from_date, to_date=to_date)


@router.post("/{state}/quota")
async def set_state_quota(
    payload: StateQuotaRequest,
    state: str = Path(..., description="State lane whose telecallers' quota to set"),
    _: AuthContext = Depends(require_permission(Permission.USERS_MANAGE)),
):
    """Set assignment_quota for every active telecaller in this state in one UPDATE
    (bulk twin of the per-user PATCH). Returns {updated, state, quota}."""
    return await stateLaneService.set_state_quota(engine, state, payload.assignment_quota)


@router.post("/{state}/distribute")
async def distribute_state(
    state: str = Path(..., description="State whose unassigned backlog to distribute"),
    ctx: AuthContext = Depends(require_permission(Permission.LEADS_MANAGE)),
):
    """Round-robin this state's live unassigned leads to its online, under-quota
    telecallers (reuses the least-loaded distributor)."""
    return await stateLaneService.distribute_state_backlog(engine, state, by_user_id=ctx.user_id)
