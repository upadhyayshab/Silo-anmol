"""Native CRM — Lead endpoints (Stage 1: Data Foundation).

Role model:
  - SUPER_ADMIN / ADMIN : see and manage all leads; bulk-distribute and delete.
  - TELECALLER          : see and edit only leads they own (auto-scoped; other leads 404);
                          may hand off a lead they own to another telecaller.

Ownership on create: a lead is attributed to whoever creates it (telecaller/admin).
System/webhook leads (no human creator) are round-robin assigned. Admins redistribute
in bulk via POST /leads/distribute; the owner is changed via POST /leads/{id}/assign.
"""
from datetime import datetime, timezone, date
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status, Response, UploadFile, File, BackgroundTasks
from fastapi.responses import JSONResponse

from config import get_settings, get_engine
from managers import LeadManager, LeadActivityManager, UserManager
from models import (
    LeadCreateRequest, LeadUpdateRequest, StageChangeRequest, NoteRequest,
    CallLogRequest, AssignRequest, DistributeRequest, DistributeResponse,
    LeadResponse, LeadDetailResponse,
    LeadActivityResponse, LeadListResponse, StatusResponse,
    LeadImportSummary, TodayQueueResponse,
)
from core.telephony import TelephonyProvider
from dependencies.telephony_dep import get_telephony_provider
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission, ScopeLevel
from utils.constants import UserRole, TELECALLER_ROLES, OWNER_ROLES
from utils.crm_constants import LeadSource
from utils.dependencies import filtering_dependency, sorting_dependency
from services import leadService, leadImportService, telephonyService, assignmentService, crmReportService

settings = get_settings()
engine = get_engine(settings.name)
lead_manager = LeadManager(engine)
activity_manager = LeadActivityManager(engine)
user_manager = UserManager(engine)

router = APIRouter(prefix="/leads", tags=["CRM - Leads"])


# --------------------------------------------------------------------------
# Dependencies / helpers
# --------------------------------------------------------------------------
# Lead row-scope keys on a single column: owner_id. A telecaller owns their
# leads (no agency nesting in Stage 1); GLOBAL/microservice callers see all.
# These mirror leadService.scope_filters_for_user / user_can_access.

def _lead_in_scope(ctx, lead) -> bool:
    if ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value:
        return True
    return getattr(lead, "owner_id", None) == ctx.user_id


async def _assert_lead_in_scope(ctx, lead):
    if _lead_in_scope(ctx, lead):
        return
    # A telecaller who handled a routed inbound call may act on a lead they don't own
    # (owner unchanged; their actions stay attributed to them via activity.user_id).
    if await assignmentService.has_call_access(engine, lead.uid, ctx.user_id):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                        detail="Access denied: this lead is outside your scope")


async def _crm_actor(ctx):
    """Resolve the authenticated user row (for name + presence) and softly bump
    last_active_at. The auth gate already happened via require_permission; this is
    just the user record the service layer / responses still need."""
    try:
        user = await user_manager.fetch(ctx.user_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    now = datetime.now(timezone.utc)
    if not user.last_active_at or (now - user.last_active_at.replace(tzinfo=timezone.utc)).total_seconds() > 300:
        # presence tracking — throttled to 5 mins to save DB writes
        await user_manager.update(ctx.user_id, {"last_active_at": now})
    return user


async def _get_lead_or_404(lead_id: str):
    """Fetch a non-deleted lead, else 404. Row-scope is asserted separately by the
    caller via _assert_lead_in_scope (403 for in-existence-but-out-of-scope)."""
    try:
        lead = await lead_manager.fetch(lead_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Lead not found")
    if lead.deleted_at is not None:
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
    fb_page_id: Optional[str] = Query(None, description="Filter to leads whose campaign_data.page_id matches this FB page"),
    filters: dict = Depends(filtering_dependency),
    sorts: list = Depends(sorting_dependency),
    ctx: AuthContext = Depends(require_permission(Permission.LEADS_READ)),
):
    """Paginated, filterable, role-scoped list of leads.

    Telecallers are auto-restricted to their own leads. Supports the standard
    `field:eq/like/in/...` filters and `field:asc/desc` sorts, plus `q` search.
    `fb_page_id` filters on campaign_data.page_id (JSON path — FB leads have no
    plain page column).
    """
    # Telecallers see leads they own OR leads they were granted call-access to (handled a
    # routed inbound call). GLOBAL/microservice see all.
    scope_owner_id, scope_uids = None, None
    if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value):
        scope_owner_id = ctx.user_id
        scope_uids = await assignmentService.call_access_lead_ids(engine, ctx.user_id)
    items, total = await lead_manager.search_leads(
        q=q, filters={**filters, "deleted_at": None}, sorts=sorts, limit=limit, offset=offset,
        scope_owner_id=scope_owner_id, scope_uids=scope_uids, fb_page_id=fb_page_id,
    )
    user_cache, outlet_cache = {}, {}
    responses = [
        await leadService.build_lead_response(
            engine, lead, user_cache=user_cache, outlet_cache=outlet_cache
        )
        for lead in items
    ]
    # Enrich each row with its latest call disposition / sub-disposition (one batched query).
    dispositions = await leadService.latest_dispositions(engine, [lead.uid for lead in items])
    for resp in responses:
        d = dispositions.get(resp.uid)
        if d:
            resp.disposition = d["disposition"]
            resp.sub_disposition = d["sub_disposition"]
    return LeadListResponse(items=responses, count=len(responses), total=total, limit=limit, offset=offset)


# --------------------------------------------------------------------------
# Today's callback queue (2.5) — registered before /{lead_id} so the static
# path is never shadowed by the lead-detail route.
# --------------------------------------------------------------------------

@router.get("/queue/today", response_model=TodayQueueResponse)
async def get_today_queue(
    owner_id: Optional[str] = Query(None, description="Admin-only: scope to one telecaller"),
    limit: int = Query(100, ge=1, le=500),
    ctx: AuthContext = Depends(require_permission(Permission.LEADS_READ)),
):
    """New / engaged / not-reachable stage buckets for the caller's leads.

    A lead leaves the queue once a call is logged today (IST) and rolls to the
    next day if still in one of these stages. Telecallers are auto-scoped to
    their own leads; admins see everyone (or one telecaller via `owner_id`).
    Soft-deleted leads are excluded.
    """
    actor = await _crm_actor(ctx)
    return await leadService.today_queue(engine, actor, owner_id=owner_id, limit=limit)


# --------------------------------------------------------------------------
# Prospect Report (superadmin CRM reports) — one flat row per prospect joining
# the lead + latest call disposition + call count + order rollup. Registered
# before /{lead_id} so "report" is not swallowed by the lead-detail route.
# --------------------------------------------------------------------------

def _report_scope_owner(ctx) -> Optional[str]:
    """Superadmin/global sees all; a scoped caller is limited to leads they own."""
    if ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value:
        return None
    return ctx.user_id


async def _build_report(ctx, *, from_date, to_date, region, owner_id, stage,
                        source, lead_numbers, limit, offset):
    nums = [n.strip() for n in lead_numbers.split(",")] if lead_numbers else None
    nums = [n for n in nums if n] if nums else None
    return await crmReportService.prospect_report(
        engine, from_date=from_date, to_date=to_date, region=region,
        owner_id=owner_id, stage=stage, source=source, lead_numbers=nums,
        scope_owner_id=_report_scope_owner(ctx), limit=limit, offset=offset,
    )


@router.get("/report")
async def prospect_report(
    from_date: Optional[date] = Query(None, description="Lead created on/after (inclusive)"),
    to_date: Optional[date] = Query(None, description="Lead created on/before (inclusive)"),
    region: Optional[str] = Query(None, description="Lead Inflow Region (lead.state)"),
    owner_id: Optional[str] = Query(None),
    stage: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    lead_numbers: Optional[str] = Query(None, description="Comma-separated prospect ids"),
    limit: int = Query(50, ge=0, le=200, description="0 = all matched rows (for client-side export)"),
    offset: int = Query(0, ge=0),
    ctx: AuthContext = Depends(require_permission(Permission.REPORTS_READ)),
):
    """Prospect report rows. Paginated for the table; pass limit=0 to fetch the
    full filtered set (capped) for the client-side Excel export."""
    rows, total = await _build_report(
        ctx, from_date=from_date, to_date=to_date, region=region, owner_id=owner_id,
        stage=stage, source=source, lead_numbers=lead_numbers, limit=limit, offset=offset,
    )
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/{lead_id}", response_model=LeadDetailResponse)
async def get_lead(lead_id: str,
                   ctx: AuthContext = Depends(require_permission(Permission.LEADS_READ))):
    """Full lead profile incl. activity timeline."""
    lead = await _get_lead_or_404(lead_id)
    await _assert_lead_in_scope(ctx, lead)
    return await leadService.build_lead_response(engine, lead, include_activities=True)


@router.get("/{lead_id}/activities", response_model=List[LeadActivityResponse])
async def list_lead_activities(
    lead_id: str,
    limit: int = Query(100, ge=1, le=500),
    ctx: AuthContext = Depends(require_permission(Permission.LEADS_READ)),
):
    """Lead activity timeline (newest first)."""
    lead = await _get_lead_or_404(lead_id)
    await _assert_lead_in_scope(ctx, lead)
    return await leadService.fetch_activities(engine, lead.uid, limit=limit)


# --------------------------------------------------------------------------
# Create (1.2, 1.3)
# --------------------------------------------------------------------------

@router.post("", response_model=LeadDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_lead(payload: LeadCreateRequest, response: Response,
                      ctx: AuthContext = Depends(require_permission(Permission.LEADS_WRITE))):
    """Create a lead, attributed to the creating user by default.

    Pass `owner_id` to assign it directly to a specific telecaller instead.

    Deduplicated: if a non-deleted lead already exists with this mobile/email,
    the incoming data is merged into it and that lead is returned with **200**
    instead of a new lead with **201**.

    Scoping: if a *telecaller* merges into a lead they don't own, the response is
    a minimal ack (`{status, detail, lead_id, owner_name}`) rather than the full
    record/timeline — the same owner-scoping the list/detail endpoints enforce.
    Admins (and the owner) get the full lead detail.
    """
    # Telecallers (incl. agency telecallers) creating a lead with no explicit
    # source have it auto-attributed to 'Telecaller' rather than left blank.
    if payload.source is None and ctx.role in TELECALLER_ROLES:
        payload.source = LeadSource.TELECALLER

    # Owner-stamping is preserved by leadService.create_lead (by_user_id attributes
    # a new lead to its creator unless payload.owner_id is set).
    lead, created = await leadService.create_lead(engine, payload, by_user_id=ctx.user_id)
    if not created:
        # Restricted ack when a scoped caller (telecaller) merges into a lead they
        # don't own; GLOBAL/admin (in-scope) get the full detail.
        if not _lead_in_scope(ctx, lead):
            owner_name = None
            if lead.owner_id:
                try:
                    owner_name = (await user_manager.fetch(lead.owner_id)).full_name
                except Exception:
                    owner_name = None
            return JSONResponse(status_code=status.HTTP_200_OK, content={
                "status": "merged",
                "detail": "A lead with this contact already exists and has been updated.",
                "lead_id": lead.uid,
                "owner_name": owner_name,
            })
        response.status_code = status.HTTP_200_OK
    return await leadService.build_lead_response(engine, lead, include_activities=True)


@router.post("/import", response_model=LeadImportSummary)
async def import_leads(
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(require_permission(Permission.LEADS_MANAGE)),
):
    """Bulk-import leads from a CSV (LSQ export or native). Admin only.

    Accepts both LeadSquared export headers and our native column names. Each
    row is deduplicated (merged into an existing lead if the phone/email already
    exists). Imported leads are round-robin assigned across telecallers.
    Returns a summary with per-row errors.
    """
    if file.content_type and "csv" not in file.content_type and \
            not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv file")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    summary = await leadImportService.import_leads_csv(engine, content, by_user_id=ctx.user_id)
    return LeadImportSummary(**summary)


# --------------------------------------------------------------------------
# Edit field / stage / note (1.5)
# --------------------------------------------------------------------------

@router.patch("/{lead_id}", response_model=LeadDetailResponse)
async def update_lead(lead_id: str, payload: LeadUpdateRequest,
                      ctx: AuthContext = Depends(require_permission(Permission.LEADS_WRITE))):
    """Patch editable fields. Logs a single FIELD_UPDATE diff to the timeline."""
    lead = await _get_lead_or_404(lead_id)
    await _assert_lead_in_scope(ctx, lead)
    changes = payload.model_dump(exclude_unset=True)
    await leadService.update_lead(engine, lead, changes, by_user_id=ctx.user_id)
    fresh = await lead_manager.fetch(lead_id)
    return await leadService.build_lead_response(engine, fresh, include_activities=True)


@router.post("/{lead_id}/stage", response_model=LeadDetailResponse)
async def change_lead_stage(lead_id: str, payload: StageChangeRequest,
                            ctx: AuthContext = Depends(require_permission(Permission.LEADS_WRITE))):
    """Change a lead's stage. Logs STAGE_CHANGE (from -> to)."""
    lead = await _get_lead_or_404(lead_id)
    await _assert_lead_in_scope(ctx, lead)
    await leadService.change_stage(engine, lead, payload.stage, by_user_id=ctx.user_id, note=payload.note)
    fresh = await lead_manager.fetch(lead_id)
    return await leadService.build_lead_response(engine, fresh, include_activities=True)


@router.post("/{lead_id}/notes", response_model=LeadActivityResponse, status_code=status.HTTP_201_CREATED)
async def add_lead_note(lead_id: str, payload: NoteRequest,
                        ctx: AuthContext = Depends(require_permission(Permission.LEADS_WRITE))):
    """Add a free-text note to the lead's timeline."""
    lead = await _get_lead_or_404(lead_id)
    await _assert_lead_in_scope(ctx, lead)
    actor = await _crm_actor(ctx)
    activity = await leadService.add_note(engine, lead.uid, payload.body, by_user_id=ctx.user_id)
    resp = LeadActivityResponse.model_validate(activity)
    resp.user_name = actor.full_name
    return resp


@router.post("/{lead_id}/calls", response_model=LeadActivityResponse, status_code=status.HTTP_201_CREATED)
async def log_lead_call(lead_id: str, payload: CallLogRequest, background_tasks: BackgroundTasks,
                        ctx: AuthContext = Depends(require_permission(Permission.LEADS_WRITE)),
                        provider: TelephonyProvider = Depends(get_telephony_provider)):
    """Log a call's disposition (outcome + optional follow-up). For softphone calls the
    body carries the Exotel `call_sid`; we then pull the CDR (recording + real duration)
    in the background and fold it into THIS entry — one call, one timeline row."""
    lead = await _get_lead_or_404(lead_id)
    await _assert_lead_in_scope(ctx, lead)
    actor = await _crm_actor(ctx)
    activity = await leadService.log_call(
        engine, lead, payload.outcome.value if payload.outcome else None,
        payload.note, payload.follow_up_at,
        by_user_id=ctx.user_id, duration_seconds=payload.duration_seconds,
        disposition=payload.disposition, sub_disposition=payload.sub_disposition,
        direction=payload.direction, call_sid=payload.call_sid,
    )
    if payload.call_sid:
        background_tasks.add_task(
            telephonyService.enrich_disposition_with_cdr, engine, provider,
            activity_uid=activity.uid, call_sid=payload.call_sid, direction=payload.direction)
    resp = LeadActivityResponse.model_validate(activity)
    resp.user_name = actor.full_name
    return resp


# --------------------------------------------------------------------------
# Reassign / distribute / delete
# --------------------------------------------------------------------------

@router.post("/distribute", response_model=DistributeResponse)
async def distribute_leads(payload: DistributeRequest,
                           ctx: AuthContext = Depends(require_permission(Permission.LEADS_MANAGE))):
    """Bulk round-robin distribution of leads across telecallers. Admin only.

    Pass `lead_ids` to distribute; optionally restrict the target pool with
    `telecaller_ids` (otherwise all active telecallers are used).
    """
    result = await leadService.distribute_leads(
        engine, payload.lead_ids, payload.telecaller_ids, by_user_id=ctx.user_id
    )
    return DistributeResponse(**result)


@router.post("/{lead_id}/assign", response_model=LeadDetailResponse)
async def assign_lead(lead_id: str, payload: AssignRequest,
                      ctx: AuthContext = Depends(require_permission(Permission.LEADS_WRITE))):
    """Change a lead's owner.

    - Admin/Super Admin: reassign any lead to any telecaller.
    - Telecaller: hand off a lead they own to another telecaller.
    """
    # Scope check: telecallers may only reassign leads they own (others -> 403).
    lead = await _get_lead_or_404(lead_id)
    await _assert_lead_in_scope(ctx, lead)
    try:
        telecaller = await user_manager.fetch(payload.telecaller_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Telecaller not found")
    if telecaller.role not in OWNER_ROLES or not telecaller.is_active:
        raise HTTPException(status_code=400, detail="Target user is not an active telecaller or agency admin")
    await leadService.reassign(engine, lead, payload.telecaller_id, by_user_id=ctx.user_id)
    fresh = await lead_manager.fetch(lead_id)
    return await leadService.build_lead_response(engine, fresh, include_activities=True)


@router.delete("/{lead_id}", response_model=StatusResponse)
async def delete_lead(lead_id: str,
                      ctx: AuthContext = Depends(require_permission(Permission.LEADS_MANAGE))):
    """Soft-delete a lead (sets deleted_at). Admin only."""
    try:
        await lead_manager.fetch(lead_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Lead not found")
    await lead_manager.update(lead_id, {"deleted_at": datetime.now(timezone.utc)})
    return StatusResponse(status="ok", message="Lead deleted")
