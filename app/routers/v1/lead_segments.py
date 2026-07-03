"""Native CRM — Saved Segments.

A "segment" is a named, reusable advanced-filter tree the CRM can re-run. Every
user sees the segments they own plus any segment shared to everyone
(``is_shared``). Only the owner may edit or delete their own segments.
"""
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from config import get_settings, get_engine
from managers import LeadSegmentManager, LeadSegmentSchema
from models import LeadSegmentCreate, LeadSegmentUpdate, LeadSegmentResponse
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission

settings = get_settings()
engine = get_engine(settings.name)
segment_manager = LeadSegmentManager(engine)

router = APIRouter(prefix="/crm/lead-segments", tags=["CRM - Segments"])


async def _get_owned_segment(seg_id: str, ctx: AuthContext) -> LeadSegmentSchema:
    """Fetch a live segment the caller OWNS, else raise. 404 for missing/soft-deleted
    (no existence leak); 403 for a segment that exists but belongs to someone else."""
    try:
        seg = await segment_manager.fetch(seg_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
    if seg.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
    if seg.owner_user_id != ctx.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the segment owner may modify it")
    return seg


@router.get("", response_model=List[LeadSegmentResponse])
async def list_segments(ctx: AuthContext = Depends(require_permission(Permission.LEADS_READ))):
    """Segments the caller owns plus any shared segment (newest first).
    Soft-deleted segments are excluded."""
    return await segment_manager.list_visible(ctx.user_id)


@router.post("", response_model=LeadSegmentResponse, status_code=status.HTTP_201_CREATED)
async def create_segment(payload: LeadSegmentCreate,
                         ctx: AuthContext = Depends(require_permission(Permission.LEADS_READ))):
    """Create a segment owned by the calling user."""
    seg = LeadSegmentSchema(
        name=payload.name,
        filter=payload.filter,
        owner_user_id=ctx.user_id,
        is_shared=bool(payload.is_shared),          # None -> False
        surface=payload.surface or "both",
    )
    return await segment_manager.create(seg)


@router.patch("/{seg_id}", response_model=LeadSegmentResponse)
async def update_segment(seg_id: str, payload: LeadSegmentUpdate,
                         ctx: AuthContext = Depends(require_permission(Permission.LEADS_READ))):
    """Patch a segment. Only the owner may edit (else 404/403)."""
    await _get_owned_segment(seg_id, ctx)
    changes = payload.model_dump(exclude_unset=True)
    if changes:
        await segment_manager.update(seg_id, changes)
    return await segment_manager.fetch(seg_id)


@router.delete("/{seg_id}")
async def delete_segment(seg_id: str,
                         ctx: AuthContext = Depends(require_permission(Permission.LEADS_READ))):
    """Soft-delete a segment (sets deleted_at). Only the owner may delete."""
    await _get_owned_segment(seg_id, ctx)
    await segment_manager.update(seg_id, {"deleted_at": datetime.now(timezone.utc)})
    return {"status": "deleted"}
