from fastapi import APIRouter, HTTPException, Depends, status, Query
from typing import Optional
from datetime import datetime, date
from decimal import Decimal

from config import get_settings, get_engine
from managers import (
    OutletManagerPayoutManager, OutletManager, UserManager,
    OutletManagerPayoutSchema
)
from models import (
    PayoutCreateRequest, PayoutUpdateRequest, PayoutStatusUpdateRequest,
    PayoutResponse, ListResponse, StatusResponse
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, PayoutStatus

settings = get_settings()
engine = get_engine(settings.name)

payout_manager = OutletManagerPayoutManager(engine)
outlet_manager = OutletManager(engine)
user_manager = UserManager(engine)

router = APIRouter(prefix="/outlet-payouts", tags=["Outlet Manager Payouts"])


@router.post("", response_model=PayoutResponse)
async def create_payout(
    payload: PayoutCreateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT
    ))
):
    """
    Create a new outlet manager payout record
    
    Access: SUPER_ADMIN, ADMIN, ACCOUNTANT
    """
    try:
        # Validate outlet exists
        try:
            outlet = await outlet_manager.fetch(payload.outlet_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Outlet not found: {payload.outlet_id}"
            )
        
        # Validate outlet manager exists and has OUTLET_MANAGER role
        try:
            manager = await user_manager.fetch(payload.outlet_manager_id)
            if manager.role != UserRole.OUTLET_MANAGER:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"User is not an outlet manager"
                )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Outlet manager not found: {payload.outlet_manager_id}"
            )
        
        # Validate manager is assigned to the outlet
        if outlet.manager_id != payload.outlet_manager_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Manager {manager.full_name} is not assigned to outlet {outlet.outlet_name}"
            )
        
        # Validate period dates
        if payload.period_to < payload.period_from:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="period_to must be after period_from"
            )
        
        # Check for overlapping payouts
        existing_payouts = await payout_manager.fetch_all(
            filters={
                "outlet_manager_id": payload.outlet_manager_id,
                "outlet_id": payload.outlet_id
            }
        )
        
        for existing in existing_payouts.items:
            # Check if periods overlap
            if not (payload.period_to < existing.period_from or payload.period_from > existing.period_to):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Payout period overlaps with existing payout (ID: {existing.uid})"
                )
        
        # Create payout record
        payout_data = OutletManagerPayoutSchema(
            outlet_id=payload.outlet_id,
            outlet_manager_id=payload.outlet_manager_id,
            period_from=payload.period_from,
            period_to=payload.period_to,
            amount=payload.amount,
            payment_date=payload.payment_date,
            payment_method=payload.payment_method,
            transaction_id=payload.transaction_id,
            remarks=payload.remarks,
            status=PayoutStatus.PENDING,
            created_by=current_user_id,
            approved_by=None,
            approved_at=None,
            paid_by=None,
            paid_at=None
        )
        
        payout = await payout_manager.create(payout_data)
        
        return PayoutResponse(
            uid=payout.uid,
            outlet_id=payout.outlet_id,
            outlet_manager_id=payout.outlet_manager_id,
            period_from=payout.period_from,
            period_to=payout.period_to,
            amount=payout.amount,
            payment_date=payout.payment_date,
            payment_method=payout.payment_method,
            transaction_id=payout.transaction_id,
            status=payout.status,
            remarks=payout.remarks,
            created_by=payout.created_by,
            approved_by=payout.approved_by,
            approved_at=payout.approved_at,
            paid_by=payout.paid_by,
            paid_at=payout.paid_at,
            created_at=payout.created_at,
            updated_at=payout.updated_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create payout: {str(e)}"
        )


@router.get("", response_model=ListResponse[PayoutResponse])
async def list_payouts(
    outlet_id: Optional[str] = Query(None, description="Filter by outlet"),
    outlet_manager_id: Optional[str] = Query(None, description="Filter by outlet manager"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status (PENDING, APPROVED, PAID, REJECTED)"),
    period_from: Optional[date] = Query(None, description="Filter by period start date"),
    period_to: Optional[date] = Query(None, description="Filter by period end date"),
    payment_date_from: Optional[date] = Query(None, description="Filter by payment date from"),
    payment_date_to: Optional[date] = Query(None, description="Filter by payment date to"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT, UserRole.OUTLET_MANAGER
    ))
):
    """
    List outlet manager payouts with filters
    
    Access: 
    - SUPER_ADMIN, ADMIN, ACCOUNTANT: Can view all payouts
    - OUTLET_MANAGER: Can only view their own payouts
    """
    try:
        # Get current user to check role
        current_user = await user_manager.fetch(current_user_id)
        
        # Build filters
        filters = {}
        
        # Outlet managers can only see their own payouts
        if current_user.role == UserRole.OUTLET_MANAGER:
            filters["outlet_manager_id"] = current_user_id
        else:
            # Admins can filter by outlet_manager_id if provided
            if outlet_manager_id:
                filters["outlet_manager_id"] = outlet_manager_id
        
        if outlet_id:
            filters["outlet_id"] = outlet_id
        
        # Handle status filter - convert string to enum
        if status_filter:
            try:
                status_enum = PayoutStatus(status_filter)
                filters["status"] = status_enum
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status. Must be one of: PENDING, APPROVED, PAID, REJECTED"
                )
        
        # Fetch payouts
        payouts = await payout_manager.fetch_all(
            limit=limit if not (period_from or period_to or payment_date_from or payment_date_to) else 0,
            offset=offset if not (period_from or period_to or payment_date_from or payment_date_to) else 0,
            filters=filters if filters else None,
            sorts=["-period_to", "-created_at"]
        )
        
        # Apply date filtering in Python if needed
        filtered_items = list(payouts.items) if payouts.items else []
        
        if period_from or period_to:
            filtered_items = [
                p for p in filtered_items
                if (not period_from or p.period_from >= period_from) and
                   (not period_to or p.period_to <= period_to)
            ]
        
        if payment_date_from or payment_date_to:
            filtered_items = [
                p for p in filtered_items
                if p.payment_date and
                   (not payment_date_from or p.payment_date >= payment_date_from) and
                   (not payment_date_to or p.payment_date <= payment_date_to)
            ]
        
        # Apply pagination after filtering
        if period_from or period_to or payment_date_from or payment_date_to:
            filtered_items = filtered_items[offset:offset + limit] if limit > 0 else filtered_items
        
        # Convert to response models
        items = []
        for p in filtered_items:
            try:
                items.append(PayoutResponse(
                    uid=p.uid,
                    outlet_id=p.outlet_id,
                    outlet_manager_id=p.outlet_manager_id,
                    period_from=p.period_from,
                    period_to=p.period_to,
                    amount=p.amount,
                    payment_date=p.payment_date,
                    payment_method=p.payment_method,
                    transaction_id=p.transaction_id,
                    status=p.status,
                    remarks=p.remarks,
                    created_by=p.created_by,
                    approved_by=p.approved_by,
                    approved_at=p.approved_at,
                    paid_by=p.paid_by,
                    paid_at=p.paid_at,
                    created_at=p.created_at,
                    updated_at=p.updated_at
                ))
            except Exception as item_error:
                # Log the error but continue processing other items
                print(f"Error processing payout {p.uid}: {str(item_error)}")
                continue
        
        return ListResponse(items=items, count=len(items))
        
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch payouts: {str(e)}"
        )


@router.get("/{payout_id}", response_model=PayoutResponse)
async def get_payout(
    payout_id: str,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT, UserRole.OUTLET_MANAGER
    ))
):
    """
    Get a single payout record
    
    Access:
    - SUPER_ADMIN, ADMIN, ACCOUNTANT: Can view any payout
    - OUTLET_MANAGER: Can only view their own payouts
    """
    try:
        payout = await payout_manager.fetch(payout_id)
        
        # Get current user to check role
        current_user = await user_manager.fetch(current_user_id)
        
        # Outlet managers can only view their own payouts
        if current_user.role == UserRole.OUTLET_MANAGER:
            if payout.outlet_manager_id != current_user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view your own payouts"
                )
        
        return PayoutResponse(
            uid=payout.uid,
            outlet_id=payout.outlet_id,
            outlet_manager_id=payout.outlet_manager_id,
            period_from=payout.period_from,
            period_to=payout.period_to,
            amount=payout.amount,
            payment_date=payout.payment_date,
            payment_method=payout.payment_method,
            transaction_id=payout.transaction_id,
            status=payout.status,
            remarks=payout.remarks,
            created_by=payout.created_by,
            approved_by=payout.approved_by,
            approved_at=payout.approved_at,
            paid_by=payout.paid_by,
            paid_at=payout.paid_at,
            created_at=payout.created_at,
            updated_at=payout.updated_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payout not found: {str(e)}"
        )


@router.patch("/{payout_id}", response_model=PayoutResponse)
async def update_payout(
    payout_id: str,
    payload: PayoutUpdateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT
    ))
):
    """
    Update payout details
    
    Can only update if status is PENDING
    
    Access: SUPER_ADMIN, ADMIN, ACCOUNTANT
    """
    try:
        # Fetch current payout
        payout = await payout_manager.fetch(payout_id)
        
        # Can only update if status is PENDING
        if payout.status != PayoutStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot update payout with status {payout.status.value}. Only PENDING payouts can be updated."
            )
        
        # Prepare updates
        updates = payload.dict(exclude_unset=True)
        
        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update"
            )
        
        # Update payout
        await payout_manager.update(payout_id, updates)
        
        # Fetch fresh copy
        updated_payout = await payout_manager.fetch(payout_id)
        
        return PayoutResponse(
            uid=updated_payout.uid,
            outlet_id=updated_payout.outlet_id,
            outlet_manager_id=updated_payout.outlet_manager_id,
            period_from=updated_payout.period_from,
            period_to=updated_payout.period_to,
            amount=updated_payout.amount,
            payment_date=updated_payout.payment_date,
            payment_method=updated_payout.payment_method,
            transaction_id=updated_payout.transaction_id,
            status=updated_payout.status,
            remarks=updated_payout.remarks,
            created_by=updated_payout.created_by,
            approved_by=updated_payout.approved_by,
            approved_at=updated_payout.approved_at,
            paid_by=updated_payout.paid_by,
            paid_at=updated_payout.paid_at,
            created_at=updated_payout.created_at,
            updated_at=updated_payout.updated_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update payout: {str(e)}"
        )


@router.patch("/{payout_id}/status", response_model=PayoutResponse)
async def update_payout_status(
    payout_id: str,
    payload: PayoutStatusUpdateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT
    ))
):
    """
    Update payout status
    
    Valid status transitions:
    - PENDING → APPROVED (sets approved_by, approved_at)
    - PENDING → REJECTED
    - APPROVED → PAID (sets paid_by, paid_at)
    - REJECTED → PENDING (reset for reprocessing)
    - PAID → (no transitions allowed)
    
    Access: SUPER_ADMIN, ADMIN, ACCOUNTANT
    """
    try:
        # Fetch current payout
        payout = await payout_manager.fetch(payout_id)
        
        current_status = payout.status
        new_status = payload.status
        
        # Define valid transitions
        valid_transitions = {
            PayoutStatus.PENDING: [PayoutStatus.APPROVED, PayoutStatus.REJECTED],
            PayoutStatus.APPROVED: [PayoutStatus.PAID],
            PayoutStatus.REJECTED: [PayoutStatus.PENDING],
            PayoutStatus.PAID: []  # Cannot change from PAID
        }
        
        # Validate transition
        if new_status not in valid_transitions.get(current_status, []):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status transition: {current_status.value} → {new_status.value}"
            )
        
        # Prepare updates
        updates = {"status": new_status}
        
        # Update remarks if provided
        if payload.remarks:
            updates["remarks"] = payload.remarks
        
        # Set audit fields based on new status
        if new_status == PayoutStatus.APPROVED:
            updates["approved_by"] = current_user_id
            updates["approved_at"] = datetime.utcnow()
        
        elif new_status == PayoutStatus.PAID:
            # Require payment_date when marking as PAID
            if not payout.payment_date:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="payment_date is required before marking payout as PAID. Update the payout first."
                )
            updates["paid_by"] = current_user_id
            updates["paid_at"] = datetime.utcnow()
        
        elif new_status == PayoutStatus.PENDING:
            # Reset approval fields when moving back to PENDING
            updates["approved_by"] = None
            updates["approved_at"] = None
        
        # Update payout
        await payout_manager.update(payout_id, updates)
        
        # Fetch fresh copy
        updated_payout = await payout_manager.fetch(payout_id)
        
        return PayoutResponse(
            uid=updated_payout.uid,
            outlet_id=updated_payout.outlet_id,
            outlet_manager_id=updated_payout.outlet_manager_id,
            period_from=updated_payout.period_from,
            period_to=updated_payout.period_to,
            amount=updated_payout.amount,
            payment_date=updated_payout.payment_date,
            payment_method=updated_payout.payment_method,
            transaction_id=updated_payout.transaction_id,
            status=updated_payout.status,
            remarks=updated_payout.remarks,
            created_by=updated_payout.created_by,
            approved_by=updated_payout.approved_by,
            approved_at=updated_payout.approved_at,
            paid_by=updated_payout.paid_by,
            paid_at=updated_payout.paid_at,
            created_at=updated_payout.created_at,
            updated_at=updated_payout.updated_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update payout status: {str(e)}"
        )


@router.delete("/{payout_id}", response_model=StatusResponse)
async def delete_payout(
    payout_id: str,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN
    ))
):
    """
    Delete a payout record
    
    Can only delete if status is PENDING or REJECTED
    Cannot delete APPROVED or PAID payouts
    
    Access: SUPER_ADMIN, ADMIN only
    """
    try:
        # Fetch payout
        payout = await payout_manager.fetch(payout_id)
        
        # Can only delete PENDING or REJECTED payouts
        if payout.status not in [PayoutStatus.PENDING, PayoutStatus.REJECTED]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete payout with status {payout.status.value}. Only PENDING or REJECTED payouts can be deleted."
            )
        
        # Delete using direct session delete
        async with payout_manager.session_factory() as session:
            import sqlalchemy as db
            query = db.select(OutletManagerPayoutSchema).filter_by(uid=payout_id)
            result = await session.execute(query)
            payout_to_delete = result.scalar_one_or_none()
            
            if not payout_to_delete:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Payout not found"
                )
            
            await session.delete(payout_to_delete)
            await session.commit()
        
        return StatusResponse(
            status="ok",
            message=f"Payout for period {payout.period_from} to {payout.period_to} deleted successfully"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete payout: {str(e)}"
        )
