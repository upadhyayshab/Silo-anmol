from fastapi import APIRouter, HTTPException, Depends, status, Query
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import select, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from config import get_settings, get_engine
from managers import (
    DeliveryGuyHandoverManager, DeliveryGuyHandoverSchema,
    OrderTransactionManager, DeliveryGuyManager, UserManager, UserSchema, OutletManager, OutletSchema, CustomerOrderManager
)
from models import (
    DeliveryHandoverCreateRequest, DeliveryHandoverStatusUpdateRequest,
    DeliveryHandoverResponse, DeliveryGuyCashBalanceResponse,
    ListResponse, StatusResponse, UserResponse, OutletResponse
)
from utils.auth import require_permission, get_auth_context, apply_scope, AuthContext
from utils.permissions import Permission, ScopeLevel
from utils.constants import OutletCollectionStatus, PaymentStatus, PaymentMethod, OrderStatus

settings = get_settings()
engine = get_engine(settings.name)

handover_manager = DeliveryGuyHandoverManager(engine)
transaction_manager = OrderTransactionManager(engine)
order_manager = CustomerOrderManager(engine)
delivery_guy_profile_manager = DeliveryGuyManager(engine)
user_manager = UserManager(engine)
outlet_manager = OutletManager(engine)

router = APIRouter(prefix="/delivery-handovers", tags=["Delivery Guy Cash Handovers"])


def _own_confinement(ctx: AuthContext) -> Optional[str]:
    """Reads gate: broad `handovers:read` sees all in scope; `handovers:read:own`
    (delivery guy) is confined to their own records. Returns the delivery_guy_id a
    caller is locked to, or None for broad read. 403 if neither permission is held."""
    if ctx.has(Permission.HANDOVERS_READ):
        return None
    if ctx.has(Permission.HANDOVERS_READ_OWN):
        return ctx.user_id
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Missing permission: {Permission.HANDOVERS_READ.value}",
    )


@router.get("/delivery-guys/{delivery_guy_id}/cash-balance", response_model=DeliveryGuyCashBalanceResponse)
async def get_cash_balance(
    delivery_guy_id: str,
    ctx: AuthContext = Depends(get_auth_context)
):
    """
    Calculate the current cash balance for a delivery guy.
    Balance = (Total Collected from CASH orders) - (Total CONFIRMED handovers)
    """
    try:
        # Own-scoped callers (delivery guy) may only ever see their own balance.
        own = _own_confinement(ctx)
        if own is not None:
            delivery_guy_id = own
        # Scope fence: a non-GLOBAL caller (outlet mgr) may only inspect a delivery
        # guy whose outlet they cover. GLOBAL/microservice -> unrestricted.
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value):
            scope_outlet = (await apply_scope({}, ctx)).get("outlet_id")
            allowed = scope_outlet if isinstance(scope_outlet, list) else [scope_outlet]
            dg_profiles = await delivery_guy_profile_manager.fetch_all(filters={"user_id": delivery_guy_id})
            dg_outlet = dg_profiles.items[0].outlet_id if dg_profiles.items else None
            if dg_outlet not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: this delivery guy is outside your scope"
                )

        # 1. Get all DELIVERED orders by this delivery guy in CASH
        orders = await order_manager.fetch_all(
            filters={
                "delivery_person_id": delivery_guy_id,
                "order_status": OrderStatus.DELIVERED
            }
        )
        total_collected = sum((o.total_amount for o in orders.items), Decimal("0.00"))

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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate cash balance: {str(e)}"
        )


@router.post("", response_model=DeliveryHandoverResponse)
async def create_handover(
    payload: DeliveryHandoverCreateRequest,
    ctx: AuthContext = Depends(require_permission(Permission.HANDOVERS_WRITE))
):
    """
    Create a new cash handover record
    """
    try:
        # Scope fence: a non-GLOBAL caller (outlet mgr) may only record a handover
        # for an outlet they cover. GLOBAL/microservice -> unrestricted.
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value):
            scope_outlet = (await apply_scope({}, ctx)).get("outlet_id")
            allowed = scope_outlet if isinstance(scope_outlet, list) else [scope_outlet]
            if payload.outlet_id not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: this outlet is outside your scope"
                )

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
        
        # Re-fetch with joins to populate relationships and avoid DetachedInstanceError
        fresh_handover = await handover_manager.fetch(handover.uid, joins=[
            DeliveryGuyHandoverSchema.delivery_guy,
            DeliveryGuyHandoverSchema.outlet
        ])
        return DeliveryHandoverResponse.from_orm(fresh_handover)
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
    ctx: AuthContext = Depends(get_auth_context)
):
    """
    List handovers with filters using direct SQL query
    """
    try:
        # Own-scoped callers (delivery guy) only ever see their own handovers.
        own = _own_confinement(ctx)
        if own is not None:
            delivery_guy_id = own

        # Row scope: outlet mgr -> own outlet, cluster/state -> their outlets, global -> all.
        # apply_scope overrides any user-supplied outlet_id for scoped callers.
        scope_outlet = (await apply_scope({}, ctx)).get("outlet_id")

        async with AsyncSession(engine) as session:
            # 1. Base query for fetching records with JOIN to get delivery guy and outlet details
            query = select(DeliveryGuyHandoverSchema, UserSchema, OutletSchema).join(
                UserSchema, DeliveryGuyHandoverSchema.delivery_guy_id == UserSchema.uid
            ).join(
                OutletSchema, DeliveryGuyHandoverSchema.outlet_id == OutletSchema.uid
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

            # Row scope: scoped callers are confined to their outlet(s) regardless of params.
            if scope_outlet is not None:
                conditions.append(
                    DeliveryGuyHandoverSchema.outlet_id.in_(scope_outlet)
                    if isinstance(scope_outlet, list)
                    else DeliveryGuyHandoverSchema.outlet_id == scope_outlet
                )

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
            for handover_obj, user_obj, outlet_obj in rows:
                # Set relationships manually to satisfy Pydantic serialization without lazy loading
                handover_obj.delivery_guy = user_obj
                handover_obj.outlet = outlet_obj
                responses.append(DeliveryHandoverResponse.from_orm(handover_obj))

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
    ctx: AuthContext = Depends(require_permission(Permission.HANDOVERS_WRITE))
):
    """
    Update handover status (Confirm/Reject)
    """
    try:
        current_user_id = ctx.user_id
        handover = await handover_manager.fetch(handover_id)

        # Scope fence: a non-GLOBAL caller (outlet mgr) may only act on a handover
        # for an outlet they cover. GLOBAL/microservice -> unrestricted.
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value):
            scope_outlet = (await apply_scope({}, ctx)).get("outlet_id")
            allowed = scope_outlet if isinstance(scope_outlet, list) else [scope_outlet]
            if handover.outlet_id not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: this handover is outside your scope"
                )

        if handover.status == OutletCollectionStatus.CONFIRMED:
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Handover already confirmed")

        updates = {"status": payload.status}
        if payload.status == OutletCollectionStatus.CONFIRMED:
            updates["confirmed_by"] = current_user_id
            updates["confirmed_at"] = datetime.utcnow()

        updated_handover = await handover_manager.update(handover_id, updates)
        # Re-fetch with joins to populate relationships and avoid DetachedInstanceError
        fresh_handover = await handover_manager.fetch(handover_id, joins=[
            DeliveryGuyHandoverSchema.delivery_guy,
            DeliveryGuyHandoverSchema.outlet
        ])
        return DeliveryHandoverResponse.from_orm(fresh_handover)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update handover status: {str(e)}"
        )
