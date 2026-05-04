from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional, Any, Dict
from datetime import datetime, date
from decimal import Decimal
from sqlalchemy import and_, or_

from config import get_settings, get_engine
from managers import (
    RiderPayoutManager, RiderPayoutSchema, CustomerOrderManager, 
    CustomerOrderSchema, UserManager, OutletManager
)
from models import (
    RiderPayoutCreateRequest, RiderPayoutUpdateRequest, RiderPayoutResponse, 
    ListResponse, StatusResponse, RiderPayoutStatusUpdateRequest
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, OrderStatus, PayoutStatus
from utils import dependencies as D

settings = get_settings()
engine = get_engine(settings.name)
payout_manager = RiderPayoutManager(engine)
order_manager = CustomerOrderManager(engine)
user_manager = UserManager(engine)
outlet_manager = OutletManager(engine)

router = APIRouter(prefix="/rider-payouts", tags=["Rider Payout Management"])

@router.get("/pending", response_model=Dict[str, Any])
async def get_pending_payout_summary(
    rider_id: str,
    outlet_id: str,
    _: str = Depends(require_roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.OUTLET_MANAGER))
):
    """Calculate pending earnings for a rider"""
    # Filter for delivered, unpaid orders for this rider and outlet
    filters = {
        "delivery_person_id": rider_id,
        "assigned_outlet_id": outlet_id,
        "order_status": OrderStatus.DELIVERED,
        "payout_id": None
    }
    
    orders = await order_manager.fetch_all(filters=filters, limit=1000)
    
    total_pending = sum(order.rider_earning for order in orders.items)
    
    return {
        "rider_id": rider_id,
        "outlet_id": outlet_id,
        "pending_order_count": len(orders.items),
        "total_pending_amount": total_pending,
        "order_ids": [o.uid for o in orders.items]
    }

@router.post("/generate", response_model=RiderPayoutResponse)
async def generate_payout(
    payload: RiderPayoutCreateRequest,
    current_user_id: str = Depends(get_current_user_id),
    _: str = Depends(require_roles(UserRole.ADMIN, UserRole.SUPER_ADMIN))
):
    """Generate a persistent payout record and lock orders to it"""
    # 1. Fetch pending orders in the date range
    # Note: Using fetch_all with custom filters might be tricky if we need range, 
    # but for simplicity we'll fetch all pending and filter in memory or use manager's fetch_all capabilities if it supports it.
    
    unpaid_filters = {
        "delivery_person_id": payload.rider_id,
        "assigned_outlet_id": payload.outlet_id,
        "order_status": OrderStatus.DELIVERED,
        "payout_id": None
    }
    
    # We fetch orders and then filter by delivery date in the range
    all_unpaid = await order_manager.fetch_all(filters=unpaid_filters, limit=5000)
    
    eligible_orders = []
    total_amount = Decimal('0.00')
    
    for order in all_unpaid.items:
        # Check if actual_delivery_date is within period
        if order.actual_delivery_date:
            delivery_date = order.actual_delivery_date.date()
            if payload.period_from <= delivery_date <= payload.period_to:
                eligible_orders.append(order)
                total_amount += order.rider_earning

    if not eligible_orders:
        raise HTTPException(status_code=400, detail="No pending delivered orders found for this period.")

    # 2. Create the Payout record
    payout = RiderPayoutSchema(
        rider_id=payload.rider_id,
        outlet_id=payload.outlet_id,
        period_from=payload.period_from,
        period_to=payload.period_to,
        total_amount=total_amount,
        status=PayoutStatus.PENDING,
        payment_method=payload.payment_method,
        remarks=payload.remarks,
        created_by=current_user_id
    )
    
    created_payout = await payout_manager.create(payout)
    
    # 3. Link orders to this payout
    for order in eligible_orders:
        await order_manager.update(order.uid, {"payout_id": created_payout.uid})
    
    return created_payout

@router.get("", response_model=ListResponse[RiderPayoutResponse])
async def list_payout_history(
    filters: Dict[str, Any] = Depends(D.filtering_dependency),
    sorts: List[str] = Depends(D.sorting_dependency),
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(require_roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.OUTLET_MANAGER))
):
    """View payout history"""
    return await payout_manager.fetch_all(limit=limit, offset=offset, sorts=sorts, filters=filters)

@router.get("/{uid}", response_model=RiderPayoutResponse)
async def get_payout_details(
    uid: str,
    _: str = Depends(require_roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.OUTLET_MANAGER))
):
    """Get details of a specific payout"""
    try:
        return await payout_manager.fetch(uid)
    except:
        raise HTTPException(status_code=404, detail="Payout record not found")

@router.patch("/{uid}/status", response_model=RiderPayoutResponse)
async def update_payout_status(
    uid: str,
    payload: RiderPayoutStatusUpdateRequest,
    current_user_id: str = Depends(get_current_user_id),
    _: str = Depends(require_roles(UserRole.ADMIN, UserRole.SUPER_ADMIN))
):
    """Mark a payout as PAID or CANCELLED"""
    updates = {
        "status": payload.status,
        "remarks": payload.remarks
    }
    
    if payload.status == PayoutStatus.PAID:
        updates["payment_date"] = date.today()
        updates["paid_by"] = current_user_id
        
    try:
        updated = await payout_manager.update(uid, updates)
        
        # If rejected, release the orders
        if payload.status == PayoutStatus.REJECTED:
            # We need to find orders linked to this payout and set payout_id back to NULL
            # This is a bit expensive, but status change to rejected is rare.
            orders = await order_manager.fetch_all(filters={"payout_id": uid}, limit=5000)
            for order in orders.items:
                await order_manager.update(order.uid, {"payout_id": None})
        
        return updated
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
