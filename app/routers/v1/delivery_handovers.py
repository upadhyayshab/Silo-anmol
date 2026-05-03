from fastapi import APIRouter, HTTPException, Depends, status, Query
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import select, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from config import get_settings, get_engine
from managers import (
    DeliveryGuyHandoverManager, DeliveryGuyHandoverSchema,
    OrderTransactionManager, DeliveryGuyManager, UserManager, UserSchema, OutletManager
)
from models import (
    DeliveryHandoverCreateRequest, DeliveryHandoverStatusUpdateRequest,
    DeliveryHandoverResponse, DeliveryGuyCashBalanceResponse,
    ListResponse, StatusResponse, UserResponse, OutletResponse
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, OutletCollectionStatus, PaymentStatus, PaymentMethod

settings = get_settings()
engine = get_engine(settings.name)

handover_manager = DeliveryGuyHandoverManager(engine)
transaction_manager = OrderTransactionManager(engine)
delivery_guy_profile_manager = DeliveryGuyManager(engine)
user_manager = UserManager(engine)
outlet_manager = OutletManager(engine)

router = APIRouter(prefix="/delivery-handovers", tags=["Delivery Guy Cash Handovers"])


@router.get("/delivery-guys/{delivery_guy_id}/cash-balance", response_model=DeliveryGuyCashBalanceResponse)
async def get_cash_balance(
    delivery_guy_id: str,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT , allowed_scopes=["delivery:read"]))
):
    """
    Calculate the current cash balance for a delivery guy.
    Balance = (Total Collected from CASH orders) - (Total CONFIRMED handovers)
    """
    try:
        # 1. Get all transactions received by this delivery guy in CASH
        transactions = await transaction_manager.fetch_all(
            filters={
                "received_by": delivery_guy_id,
                "payment_method": PaymentMethod.CASH,
                "payment_status": PaymentStatus.PAID
            }
        )
        total_collected = sum((t.amount_paid for t in transactions.items), Decimal("0.00"))

        # 2. Get all CONFIRMED handovers by this delivery guy
        handovers = await handover_manager.fetch_all(
            filters={
                "delivery_guy_id": delivery_guy_id,
                "status": OutletCollectionStatus.CONFIRMED
            }
        )
        total_handed_over = sum((h.amount for h in handovers.items), Decimal("0.00"))

        current_balance = total_collected - total_handed_over

        return DeliveryGuyCashBalanceResponse(
            delivery_guy_id=delivery_guy_id,
            current_cash_balance=current_balance,
            total_collected=total_collected,
            total_handed_over=total_handed_over
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate cash balance: {str(e)}"
        )


@router.post("", response_model=DeliveryHandoverResponse)
async def create_handover(
    payload: DeliveryHandoverCreateRequest,
    current_user_id: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT))
):
    """
    Create a new cash handover record
    """
    try:
        # Validate delivery guy exists
        try:
            await user_manager.fetch(payload.delivery_guy_id)
        except Exception:
             raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery guy not found")

        # Validate outlet exists
        try:
            await outlet_manager.fetch(payload.outlet_id)
        except Exception:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outlet not found")

        handover_data = DeliveryGuyHandoverSchema(
            delivery_guy_id=payload.delivery_guy_id,
            outlet_id=payload.outlet_id,
            amount=payload.amount,
            handover_date=payload.handover_date,
            status=OutletCollectionStatus.PENDING,
            remarks=payload.remarks
        )

        handover = await handover_manager.create(handover_data)
        return DeliveryHandoverResponse.from_orm(handover)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create handover: {str(e)}"
        )


@router.get("", response_model=ListResponse[DeliveryHandoverResponse])
async def list_handovers(
    delivery_guy_id: Optional[str] = Query(None),
    outlet_id: Optional[str] = Query(None),
    status_filter: Optional[OutletCollectionStatus] = Query(None, alias="status"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT , allowed_scopes=["delivery:read"]))
):
    """
    List handovers with filters using direct SQL query
    """
    try:
        async with AsyncSession(engine) as session:
            # 1. Base query for fetching records with JOIN to get delivery guy details
            query = select(DeliveryGuyHandoverSchema, UserSchema).join(
                UserSchema, DeliveryGuyHandoverSchema.delivery_guy_id == UserSchema.uid
            )

            # 2. Build filters
            conditions = []
            if delivery_guy_id:
                conditions.append(DeliveryGuyHandoverSchema.delivery_guy_id == delivery_guy_id)
            if outlet_id:
                conditions.append(DeliveryGuyHandoverSchema.outlet_id == outlet_id)
            if status_filter:
                conditions.append(DeliveryGuyHandoverSchema.status == status_filter)
            if date_from:
                conditions.append(DeliveryGuyHandoverSchema.handover_date >= date_from)
            if date_to:
                conditions.append(DeliveryGuyHandoverSchema.handover_date <= date_to)

            if conditions:
                query = query.where(and_(*conditions))

            # 3. Get total count for pagination metadata
            count_query = select(func.count()).select_from(DeliveryGuyHandoverSchema)
            if conditions:
                count_query = count_query.where(and_(*conditions))
            
            total_count = (await session.execute(count_query)).scalar()

            # 4. Apply sorting and pagination
            query = query.order_by(
                desc(DeliveryGuyHandoverSchema.handover_date),
                desc(DeliveryGuyHandoverSchema.created_at)
            ).offset(offset).limit(limit)

            # 5. Execute query
            result = await session.execute(query)
            rows = result.all()

            responses = []
            for handover_obj, user_obj in rows:
                resp = DeliveryHandoverResponse.from_orm(handover_obj)
                resp.delivery_guy = UserResponse.from_orm(user_obj)
                responses.append(resp)

            return ListResponse(items=responses, count=total_count)

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch handovers: {str(e)}"
        )


@router.patch("/{handover_id}/status", response_model=DeliveryHandoverResponse)
async def update_handover_status(
    handover_id: str,
    payload: DeliveryHandoverStatusUpdateRequest,
    current_user_id: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT))
):
    """
    Update handover status (Confirm/Reject)
    """
    try:
        handover = await handover_manager.fetch(handover_id)
        
        if handover.status == OutletCollectionStatus.CONFIRMED:
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Handover already confirmed")

        updates = {"status": payload.status}
        if payload.status == OutletCollectionStatus.CONFIRMED:
            updates["confirmed_by"] = current_user_id
            updates["confirmed_at"] = datetime.utcnow()

        updated_handover = await handover_manager.update(handover_id, updates)
        # Re-fetch to get complete object
        fresh_handover = await handover_manager.fetch(handover_id)
        return DeliveryHandoverResponse.from_orm(fresh_handover)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update handover status: {str(e)}"
        )
