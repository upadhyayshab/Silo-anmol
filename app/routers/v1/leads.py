"""Native CRM — Lead endpoints (Stage 1: Data Foundation).

Role model:
  - SUPER_ADMIN / ADMIN : see and manage all leads; only they may reassign or delete.
  - TELECALLER          : see and edit only leads they own (auto-scoped; other leads 404).
"""
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status

from config import get_settings, get_engine
from managers import LeadManager, LeadActivityManager, UserManager
from models import (
    LeadCreateRequest, LeadUpdateRequest, StageChangeRequest, NoteRequest,
    CallLogRequest, AssignRequest, LeadResponse, LeadDetailResponse,
    LeadActivityResponse, LeadListResponse, StatusResponse,
)
from utils.auth import require_roles
from utils.constants import UserRole
from utils.dependencies import filtering_dependency, sorting_dependency
from services import leadService

settings = get_settings()
engine = get_engine(settings.name)
lead_manager = LeadManager(engine)
activity_manager = LeadActivityManager(engine)
user_manager = UserManager(engine)

router = APIRouter(prefix="/leads", tags=["CRM - Leads"])

CRM_ROLES = (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.TELECALLER)
ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.ADMIN)


# --------------------------------------------------------------------------
# Dependencies / helpers
# --------------------------------------------------------------------------

async def get_current_crm_user(user_id: str = Depends(require_roles(*CRM_ROLES))):
    """Resolve the authenticated user (role enforced by require_roles)."""
    try:
        return await user_manager.fetch(user_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")


async def _get_lead_or_404(lead_id: str, user):
    """Fetch a non-deleted lead the user is allowed to see, else 404."""
    try:
        lead = await lead_manager.fetch(lead_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Lead not found")
    if lead.deleted_at is not None or not leadService.user_can_access(user, lead):
        # Hide existence of out-of-scope / deleted leads.
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


# --------------------------------------------------------------------------
# List & detail (1.2, 1.4)
# --------------------------------------------------------------------------

@router.get("", response_model=LeadListResponse)
async def list_leads(
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None, description="Free-text search: name / mobile / email / lead number"),
    filters: dict = Depends(filtering_dependency),
    sorts: list = Depends(sorting_dependency),
    current_user=Depends(get_current_crm_user),
):
    """Paginated, filterable, role-scoped list of leads.

    Telecallers are auto-restricted to their own leads. Supports the standard
    `field:eq/like/in/...` filters and `field:asc/desc` sorts, plus `q` search.
    """
    merged = {**filters, **leadService.scope_filters_for_user(current_user), "deleted_at": None}
    items, total = await lead_manager.search_leads(
        q=q, filters=merged, sorts=sorts, limit=limit, offset=offset
    )
    user_cache, outlet_cache = {}, {}
    responses = [
        await leadService.build_lead_response(
            engine, lead, user_cache=user_cache, outlet_cache=outlet_cache
        )
        for lead in items
    ]
    return LeadListResponse(items=responses, count=len(responses), total=total, limit=limit, offset=offset)


@router.get("/{lead_id}", response_model=LeadDetailResponse)
async def get_lead(lead_id: str, current_user=Depends(get_current_crm_user)):
    """Full lead profile incl. activity timeline."""
    lead = await _get_lead_or_404(lead_id, current_user)
    return await leadService.build_lead_response(engine, lead, include_activities=True)


@router.get("/{lead_id}/activities", response_model=List[LeadActivityResponse])
async def list_lead_activities(
    lead_id: str,
    limit: int = Query(100, ge=1, le=500),
    current_user=Depends(get_current_crm_user),
):
    """Lead activity timeline (newest first)."""
    lead = await _get_lead_or_404(lead_id, current_user)
    return await leadService.fetch_activities(engine, lead.uid, limit=limit)


# --------------------------------------------------------------------------
# Create (1.2, 1.3)
# --------------------------------------------------------------------------

@router.post("", response_model=LeadDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_lead(payload: LeadCreateRequest, current_user=Depends(get_current_crm_user)):
    """Create a lead; resolves the serving outlet and round-robin assigns an owner."""
    lead = await leadService.create_lead(engine, payload, by_user_id=current_user.uid)
    return await leadService.build_lead_response(engine, lead, include_activities=True)


# --------------------------------------------------------------------------
# Edit field / stage / note (1.5)
# --------------------------------------------------------------------------

@router.patch("/{lead_id}", response_model=LeadDetailResponse)
async def update_lead(lead_id: str, payload: LeadUpdateRequest, current_user=Depends(get_current_crm_user)):
    """Patch editable fields. Logs a single FIELD_UPDATE diff to the timeline."""
    lead = await _get_lead_or_404(lead_id, current_user)
    changes = payload.model_dump(exclude_unset=True)
    await leadService.update_lead(engine, lead, changes, by_user_id=current_user.uid)
    fresh = await lead_manager.fetch(lead_id)
    return await leadService.build_lead_response(engine, fresh, include_activities=True)


@router.post("/{lead_id}/stage", response_model=LeadDetailResponse)
async def change_lead_stage(lead_id: str, payload: StageChangeRequest, current_user=Depends(get_current_crm_user)):
    """Change a lead's stage. Logs STAGE_CHANGE (from -> to)."""
    lead = await _get_lead_or_404(lead_id, current_user)
    await leadService.change_stage(engine, lead, payload.stage, by_user_id=current_user.uid, note=payload.note)
    fresh = await lead_manager.fetch(lead_id)
    return await leadService.build_lead_response(engine, fresh, include_activities=True)


@router.post("/{lead_id}/notes", response_model=LeadActivityResponse, status_code=status.HTTP_201_CREATED)
async def add_lead_note(lead_id: str, payload: NoteRequest, current_user=Depends(get_current_crm_user)):
    """Add a free-text note to the lead's timeline."""
    lead = await _get_lead_or_404(lead_id, current_user)
    activity = await leadService.add_note(engine, lead.uid, payload.body, by_user_id=current_user.uid)
    resp = LeadActivityResponse.model_validate(activity)
    resp.user_name = current_user.full_name
    return resp


@router.post("/{lead_id}/calls", response_model=LeadActivityResponse, status_code=status.HTTP_201_CREATED)
async def log_lead_call(lead_id: str, payload: CallLogRequest, current_user=Depends(get_current_crm_user)):
    """Log a manual call attempt (outcome + optional follow-up). Stage-2 workflow."""
    lead = await _get_lead_or_404(lead_id, current_user)
    activity = await leadService.log_call(
        engine, lead, payload.outcome.value, payload.note, payload.follow_up_at,
        by_user_id=current_user.uid,
    )
    resp = LeadActivityResponse.model_validate(activity)
    resp.user_name = current_user.full_name
    return resp


# --------------------------------------------------------------------------
# Admin: reassign & delete
# --------------------------------------------------------------------------

@router.post("/{lead_id}/assign", response_model=LeadDetailResponse)
async def assign_lead(lead_id: str, payload: AssignRequest, admin_id: str = Depends(require_roles(*ADMIN_ROLES))):
    """Manually (re)assign a lead to a telecaller. Admin only."""
    try:
        lead = await lead_manager.fetch(lead_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        telecaller = await user_manager.fetch(payload.telecaller_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Telecaller not found")
    if telecaller.role != UserRole.TELECALLER:
        raise HTTPException(status_code=400, detail="Target user is not a telecaller")
    await leadService.reassign(engine, lead, payload.telecaller_id, by_user_id=admin_id)
    fresh = await lead_manager.fetch(lead_id)
    return await leadService.build_lead_response(engine, fresh, include_activities=True)


@router.delete("/{lead_id}", response_model=StatusResponse)
async def delete_lead(lead_id: str, admin_id: str = Depends(require_roles(*ADMIN_ROLES))):
    """Soft-delete a lead (sets deleted_at). Admin only."""
    try:
        await lead_manager.fetch(lead_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Lead not found")
    await lead_manager.update(lead_id, {"deleted_at": datetime.now(timezone.utc)})
    return StatusResponse(status="ok", message="Lead deleted")
