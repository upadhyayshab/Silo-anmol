from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional
from datetime import datetime, date
from decimal import Decimal
import sqlalchemy as db
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_engine, get_settings
from managers import UserManager
from managers.erpManagers import (
    InventoryAuditSchema, InventoryAuditItemSchema, OutletSchema, ProductSchema,
    InventoryAuditManager
)
from models import ListResponse, StatusResponse
from models.erpModels import (
    WeeklyInventoryAuditResponse, WeeklyInventoryAuditItemResponse,
    WeeklyInventoryAuditSubmitRequest, WeeklyInventoryAuditSummaryResponse,
    AuditStatus
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole
from services.inventory_audit_service import inventory_audit_service

router = APIRouter(prefix="/inventory-audits", tags=["Inventory Audits"])
engine = get_engine(get_settings().name)
user_manager = UserManager(engine)
audit_manager = InventoryAuditManager(engine)

@router.post("/admin/generate", response_model=StatusResponse)
async def generate_weekly_audits(
    current_user_id: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """Manually trigger the generation of weekly audits (Admin only)."""
    try:
        result = await inventory_audit_service.generate_weekly_audits()
        return StatusResponse(status="success", message=f"Generated {result['audits_created']} audits.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("", response_model=ListResponse[WeeklyInventoryAuditResponse])
async def list_audits(
    outlet_id: Optional[str] = None,
    audit_date: Optional[date] = None,
    status: Optional[AuditStatus] = None,
    limit: int = 100,
    offset: int = 0,
    sorts: str = "-created_at",
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """List audits, filtered by outlet or date."""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        db_filters = {}
        if current_user.role == UserRole.OUTLET_MANAGER:
            if not current_user.outlet_id:
                return ListResponse(items=[], count=0)
            db_filters["outlet_id"] = current_user.outlet_id
        elif outlet_id:
            db_filters["outlet_id"] = outlet_id
            
        if audit_date:
            db_filters["audit_date"] = audit_date
            
        if status:
            db_filters["status"] = status
            
        sort_list = [s.strip() for s in sorts.split(",") if s.strip()]
        
        records = await audit_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=db_filters or None,
            sorts=sort_list,
            joins=[InventoryAuditSchema.outlet]
        )
        
        responses = []
        for audit in records.items:
            responses.append(
                WeeklyInventoryAuditResponse(
                    uid=audit.uid,
                    outlet_id=audit.outlet_id,
                    outlet_name=audit.outlet.outlet_name if audit.outlet else None,
                    audit_date=audit.audit_date,
                    status=audit.status,
                    match_percentage=audit.match_percentage,
                    submitted_at=audit.submitted_at,
                    submitted_by=audit.submitted_by,
                    items=[]
                )
            )
            
        return ListResponse(items=responses, count=records.count)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/admin/summary", response_model=WeeklyInventoryAuditSummaryResponse)
async def get_audit_summary(
    audit_date: Optional[date] = None,
    current_user_id: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """Get a summary of audits for a specific date (or today)."""
    target_date = audit_date or date.today()
    try:
        async with AsyncSession(engine) as session:
            # Total active outlets
            outlets_count = await session.scalar(
                select(func.count(OutletSchema.uid)).where(OutletSchema.is_active == True)
            )
            
            # Completed audits for date
            completed_count = await session.scalar(
                select(func.count(InventoryAuditSchema.uid))
                .where(InventoryAuditSchema.audit_date == target_date)
                .where(InventoryAuditSchema.status == AuditStatus.COMPLETED)
            )
            
            # Average match percentage
            avg_match = await session.scalar(
                select(func.avg(InventoryAuditSchema.match_percentage))
                .where(InventoryAuditSchema.audit_date == target_date)
                .where(InventoryAuditSchema.status == AuditStatus.COMPLETED)
            )
            
            return WeeklyInventoryAuditSummaryResponse(
                total_active_outlets=outlets_count or 0,
                completed_audits=completed_count or 0,
                average_match_percentage=Decimal(avg_match) if avg_match is not None else None
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{audit_id}", response_model=WeeklyInventoryAuditResponse)
async def get_audit_details(
    audit_id: str,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Get details of a specific audit including all items."""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        async with AsyncSession(engine) as session:
            audit = await session.get(InventoryAuditSchema, audit_id)
            if not audit:
                raise HTTPException(status_code=404, detail="Audit not found")
                
            if current_user.role == UserRole.OUTLET_MANAGER and audit.outlet_id != current_user.outlet_id:
                raise HTTPException(status_code=403, detail="Not authorized to view this audit")
                
            items_result = await session.execute(
                select(InventoryAuditItemSchema, ProductSchema.product_name)
                .join(ProductSchema, InventoryAuditItemSchema.product_id == ProductSchema.uid)
                .where(InventoryAuditItemSchema.audit_id == audit_id)
            )
            items_rows = items_result.all()
            
            response = WeeklyInventoryAuditResponse(
                uid=audit.uid,
                outlet_id=audit.outlet_id,
                audit_date=audit.audit_date,
                status=audit.status,
                match_percentage=audit.match_percentage,
                submitted_at=audit.submitted_at,
                submitted_by=audit.submitted_by,
                items=[
                    WeeklyInventoryAuditItemResponse(
                        uid=item.uid,
                        product_id=item.product_id,
                        product_name=name,
                        system_quantity=item.system_quantity,
                        physical_quantity=item.physical_quantity
                    )
                    for item, name in items_rows
                ]
            )
            return response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{audit_id}/submit", response_model=WeeklyInventoryAuditResponse)
async def submit_audit(
    audit_id: str,
    payload: WeeklyInventoryAuditSubmitRequest,
    current_user_id: str = Depends(require_roles(UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN))
):
    """Submit an audit with physical counts."""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        async with AsyncSession(engine) as session:
            audit = await session.get(InventoryAuditSchema, audit_id)
            if not audit:
                raise HTTPException(status_code=404, detail="Audit not found")
                
            if audit.status == AuditStatus.COMPLETED:
                raise HTTPException(status_code=400, detail="Audit is already completed and cannot be modified")
                
            if current_user.role == UserRole.OUTLET_MANAGER and audit.outlet_id != current_user.outlet_id:
                raise HTTPException(status_code=403, detail="Not authorized to submit this audit")
                
            # Get existing items
            items_result = await session.execute(
                select(InventoryAuditItemSchema).where(InventoryAuditItemSchema.audit_id == audit_id)
            )
            items = items_result.scalars().all()
            items_map = {item.product_id: item for item in items}
            
            total_items = len(items)
            matched_items = 0
            
            for submitted_item in payload.items:
                if submitted_item.product_id in items_map:
                    db_item = items_map[submitted_item.product_id]
                    db_item.physical_quantity = submitted_item.physical_quantity
                    
                    if db_item.physical_quantity == db_item.system_quantity:
                        matched_items += 1
                        
            # Update Audit status and match percentage
            audit.status = AuditStatus.COMPLETED
            audit.submitted_at = datetime.utcnow()
            audit.submitted_by = current_user.uid
            
            if total_items > 0:
                audit.match_percentage = Decimal(matched_items) / Decimal(total_items) * Decimal(100)
            else:
                audit.match_percentage = Decimal(0)
                
            response = WeeklyInventoryAuditResponse(
                uid=audit.uid,
                outlet_id=audit.outlet_id,
                audit_date=audit.audit_date,
                status=audit.status,
                match_percentage=audit.match_percentage,
                submitted_at=audit.submitted_at,
                submitted_by=audit.submitted_by,
                items=[]
            )
            
            await session.commit()
            
            return response
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
