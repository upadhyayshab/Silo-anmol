from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional, Any, Dict
from datetime import datetime, date
from decimal import Decimal
from sqlalchemy import and_, or_, func, select

from config import get_settings, get_engine
from managers import (
    RiderPayoutManager, RiderPayoutSchema, CustomerOrderManager, 
    CustomerOrderSchema, UserManager, OutletManager, DeliveryGuyManager,
    DeliveryGuySchema, UserSchema, OutletSchema
)
from models import (
    RiderPayoutCreateRequest, RiderPayoutUpdateRequest, RiderPayoutResponse, 
    ListResponse, StatusResponse, RiderPayoutStatusUpdateRequest, RiderPayoutSummaryItem
)
from utils.auth import require_permission, apply_scope, AuthContext
from utils.permissions import Permission, ScopeLevel
from utils.constants import OrderStatus, PayoutStatus, PayoutFrequency
from utils import dependencies as D
from utils.functions import ensure_date

settings = get_settings()
engine = get_engine(settings.name)
payout_manager = RiderPayoutManager(engine)
order_manager = CustomerOrderManager(engine)
user_manager = UserManager(engine)
outlet_manager = OutletManager(engine)

router = APIRouter(prefix="/rider-payouts", tags=["Rider Payout Management"])


async def _payout_scope_outlet_ids(ctx: AuthContext):
    """Outlet ids the caller may see, or None for unrestricted (GLOBAL/microservice)."""
    if ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value:
        return None
    sv = (await apply_scope({}, ctx)).get("outlet_id")
    if sv is None:
        return ["__none__"]  # scoped but no outlet dimension -> match nothing
    return sv if isinstance(sv, list) else [sv]


async def _assert_payout_in_scope(ctx: AuthContext, payout):
    """Scoped (non-global) callers may only act on a payout for an outlet in their scope."""
    scope_ids = await _payout_scope_outlet_ids(ctx)
    if scope_ids is None or payout.outlet_id in scope_ids:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access denied: this payout is outside your scope",
    )


@router.get("/summary", response_model=List[RiderPayoutSummaryItem])
async def get_rider_payout_summary(
    frequency: Optional[PayoutFrequency] = None,
    outlet_id: Optional[str] = None,
    ctx: AuthContext = Depends(require_permission(Permission.PAYOUTS_READ))
):
    """
    Get a summary of pending payouts for all riders.
    Includes pending earnings, last payout info, and frequency.
    """
    scope_ids = await _payout_scope_outlet_ids(ctx)
    async with engine.begin() as conn:
        # 1. Base query for all delivery guys
        query = (
            select(
                DeliveryGuySchema.user_id.label("rider_id"),
                UserSchema.full_name,
                UserSchema.phone,
                DeliveryGuySchema.outlet_id,
                OutletSchema.outlet_name,
                DeliveryGuySchema.payout_frequency
            )
            .join(UserSchema, DeliveryGuySchema.user_id == UserSchema.uid)
            .join(OutletSchema, DeliveryGuySchema.outlet_id == OutletSchema.uid)
            .where(DeliveryGuySchema.is_deleted == False)
            .where(DeliveryGuySchema.is_active_for_delivery == True)
        )
        
        if frequency:
            query = query.where(DeliveryGuySchema.payout_frequency == frequency)
        if outlet_id:
            query = query.where(DeliveryGuySchema.outlet_id == outlet_id)
        # OUTLET-scoped callers see only riders at outlets in their scope
        if scope_ids is not None:
            query = query.where(DeliveryGuySchema.outlet_id.in_(scope_ids))

        result = await conn.execute(query)
        riders = result.fetchall()
        
        summary = []
        for r in riders:
            # 2. Get pending orders for this rider
            pending_query = (
                select(
                    func.sum(CustomerOrderSchema.rider_earning).label("total_pending"),
                    func.count(CustomerOrderSchema.uid).label("order_count")
                )
                .where(CustomerOrderSchema.delivery_person_id == r.rider_id)
                .where(CustomerOrderSchema.order_status == OrderStatus.DELIVERED)
                .where(CustomerOrderSchema.payout_id == None)
            )
            pending_res = await conn.execute(pending_query)
            pending_data = pending_res.fetchone()
            
            # 3. Get last payout info
            last_payout_query = (
                select(
                    RiderPayoutSchema.payment_date,
                    RiderPayoutSchema.total_amount
                )
                .where(RiderPayoutSchema.rider_id == r.rider_id)
                .order_by(RiderPayoutSchema.created_at.desc())
                .limit(1)
            )
            last_payout_res = await conn.execute(last_payout_query)
            last_payout = last_payout_res.fetchone()
            
            summary.append(RiderPayoutSummaryItem(
                rider_id=r.rider_id,
                full_name=r.full_name,
                phone=r.phone,
                outlet_id=r.outlet_id,
                outlet_name=r.outlet_name,
                payout_frequency=r.payout_frequency,
                pending_amount=pending_data.total_pending or Decimal('0.00'),
                pending_order_count=pending_data.order_count or 0,
                last_payout_date=last_payout.payment_date if last_payout else None,
                last_payout_amount=last_payout.total_amount if last_payout else None
            ))
            
    return summary

@router.get("/pending", response_model=Dict[str, Any])
async def get_pending_payout_summary(
    rider_id: str,
    outlet_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.PAYOUTS_READ))
):
    """Calculate pending earnings for a rider"""
    # OUTLET-scoped callers may only query their own outlet
    scope_ids = await _payout_scope_outlet_ids(ctx)
    if scope_ids is not None and outlet_id not in scope_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: this outlet is outside your scope",
        )
    # Filter for delivered, unpaid orders for this rider and outlet
    filters = {
        "delivery_person_id": rider_id,
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
    ctx: AuthContext = Depends(require_permission(Permission.PAYOUTS_WRITE))
):
    """Generate a persistent payout record and lock orders to it"""
    current_user_id = ctx.user_id
    # 1. Fetch pending orders in the date range
    # Note: Using fetch_all with custom filters might be tricky if we need range, 
    # but for simplicity we'll fetch all pending and filter in memory or use manager's fetch_all capabilities if it supports it.
    
    unpaid_filters = {
        "delivery_person_id": payload.rider_id,
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
            delivery_date = ensure_date(order.actual_delivery_date)
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
        transaction_id=payload.transaction_id,
        remarks=payload.remarks,
        created_by=current_user_id
    )
    
    created_payout = await payout_manager.create(payout)
    
    # 3. Link orders to this payout
    for order in eligible_orders:
        await order_manager.update(order.uid, {"payout_id": created_payout.uid})
    
    # Fetch with joins to avoid DetachedInstanceError during serialization
    return await payout_manager.fetch(created_payout.uid, joins=[RiderPayoutSchema.rider, RiderPayoutSchema.outlet])

@router.get("", response_model=ListResponse[RiderPayoutResponse])
async def list_payout_history(
    filters: Dict[str, Any] = Depends(D.filtering_dependency),
    sorts: List[str] = Depends(D.sorting_dependency),
    limit: int = 50,
    offset: int = 0,
    ctx: AuthContext = Depends(require_permission(Permission.PAYOUTS_READ))
):
    """View payout history"""
    filters = await apply_scope(filters, ctx)
    return await payout_manager.fetch_all(
        limit=limit,
        offset=offset,
        sorts=sorts,
        filters=filters,
        joins=[RiderPayoutSchema.rider, RiderPayoutSchema.outlet]
    )
@router.get("/rider/{rider_id}", response_model=ListResponse[RiderPayoutResponse])
async def list_payout_history(
    rider_id: str,
    filters: Dict[str, Any] = Depends(D.filtering_dependency),
    sorts: List[str] = Depends(D.sorting_dependency),
    limit: int = 50,
    offset: int = 0,
    ctx: AuthContext = Depends(require_permission(Permission.PAYOUTS_READ))
):
    """View payout history for a specific rider"""
    if rider_id:
        filters["rider_id"] = rider_id
    filters = await apply_scope(filters, ctx)
    return await payout_manager.fetch_all(
        limit=limit,
        offset=offset,
        sorts=sorts,
        filters=filters,
        joins=[RiderPayoutSchema.rider, RiderPayoutSchema.outlet]
    )

@router.get("/{uid}", response_model=RiderPayoutResponse)
async def get_payout_details(
    uid: str,
    ctx: AuthContext = Depends(require_permission(Permission.PAYOUTS_READ))
):
    """Get details of a specific payout"""
    try:
        payout = await payout_manager.fetch(uid, joins=[RiderPayoutSchema.rider, RiderPayoutSchema.outlet])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="Payout record not found")
    # Scoped callers may only view payouts for an outlet in their scope
    await _assert_payout_in_scope(ctx, payout)
    return payout

@router.patch("/{uid}/status", response_model=RiderPayoutResponse)
async def update_payout_status(
    uid: str,
    payload: RiderPayoutStatusUpdateRequest,
    ctx: AuthContext = Depends(require_permission(Permission.PAYOUTS_APPROVE))
):
    """Mark a payout as PAID or CANCELLED"""
    current_user_id = ctx.user_id
    updates = {
        "status": payload.status,
        "remarks": payload.remarks
    }
    
    if payload.status == PayoutStatus.PAID:
        updates["payment_date"] = date.today()
        updates["paid_by"] = current_user_id
        
    try:
        updated = await payout_manager.update(
            uid, 
            updates, 
            joins=[RiderPayoutSchema.rider, RiderPayoutSchema.outlet]
        )
        
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
