from fastapi import APIRouter, HTTPException, Depends, status, Query
from typing import Optional
from datetime import datetime, date
from decimal import Decimal

from config import get_settings, get_engine
from managers import (
    OutletDailyCollectionManager, OutletManager, UserManager,
    OutletDailyCollectionSchema, CustomerOrderManager
)
from models import (
    OutletCollectionCreateRequest, OutletCollectionStatusUpdateRequest,
    OutletCollectionResponse, OutletCollectionSummaryResponse,
    ListResponse, StatusResponse
)
from utils.auth import require_permission, apply_scope, AuthContext
from utils.permissions import Permission, ScopeLevel
from utils.constants import OutletCollectionStatus, OrderStatus
from utils.functions import ensure_date

settings = get_settings()
engine = get_engine(settings.name)

collection_manager = OutletDailyCollectionManager(engine)
outlet_manager = OutletManager(engine)
user_manager = UserManager(engine)
order_manager = CustomerOrderManager(engine)

router = APIRouter(prefix="/outlet-collections", tags=["Outlet Collections"])


async def _collection_scope_ids(ctx: AuthContext):
    """Outlet-ids the caller may touch, or None for unrestricted (GLOBAL/microservice)."""
    if ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value:
        return None
    sv = (await apply_scope({}, ctx)).get("outlet_id")
    if sv is None:
        return ["__none__"]  # scoped but no outlet dimension -> match nothing
    return sv if isinstance(sv, list) else [sv]


async def _assert_collection_in_scope(ctx: AuthContext, collection):
    """Scoped (non-global) callers may only act on collections for their outlet(s)."""
    scope_ids = await _collection_scope_ids(ctx)
    if scope_ids is None:
        return
    if collection.outlet_id in scope_ids:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access denied: this collection is outside your scope",
    )


async def _resolve_user_names(user_ids):
    """Map user uid -> full_name for a set of ids (skips falsy, dedupes)."""
    names = {}
    for uid in {u for u in user_ids if u}:
        try:
            user = await user_manager.fetch(uid)
            names[uid] = getattr(user, "full_name", None)
        except Exception:
            names[uid] = None
    return names


def _to_response(c, names=None) -> OutletCollectionResponse:
    """Build the API response for one collection row, filling in actor names."""
    names = names or {}
    return OutletCollectionResponse(
        uid=c.uid,
        collection_date=c.date,
        outlet_id=c.outlet_id,
        amount=c.amount,
        payment_mode=c.payment_mode,
        payment_sub_mode=c.payment_sub_mode,
        transaction_id=c.transaction_id,
        remarks=c.remarks,
        confirmation_status=c.confirmation_status,
        created_by=c.created_by,
        created_by_name=names.get(c.created_by),
        confirmed_by=c.confirmed_by,
        confirmed_by_name=names.get(c.confirmed_by),
        confirmed_at=c.confirmed_at,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


@router.post("", response_model=OutletCollectionResponse)
async def create_collection(
    payload: OutletCollectionCreateRequest,
    ctx: AuthContext = Depends(require_permission(Permission.COLLECTIONS_WRITE)),
):
    """
    Create a new outlet daily collection record

    Access: holders of collections:write. Outlet managers record only their own
    outlet's collection (outlet_id forced to their assigned outlet).
    """
    try:
        # Non-GLOBAL callers may only record for their own outlet
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value):
            if not ctx.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not assigned to any outlet",
                )
            payload.outlet_id = ctx.outlet_id

        # Global callers (finance/admin) must name the outlet explicitly.
        if not payload.outlet_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="outlet_id is required",
            )

        # Validate outlet exists
        try:
            await outlet_manager.fetch(payload.outlet_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Outlet not found: {payload.outlet_id}"
            )
        
        # Create collection record
        collection_data = OutletDailyCollectionSchema(
            date=payload.collection_date,
            outlet_id=payload.outlet_id,
            amount=payload.amount,
            payment_mode=payload.payment_mode,
            payment_sub_mode=payload.payment_sub_mode,
            transaction_id=payload.transaction_id,
            remarks=payload.remarks,
            confirmation_status=OutletCollectionStatus.PENDING,
            created_by=ctx.user_id,
            confirmed_by=None,
            confirmed_at=None
        )

        collection = await collection_manager.create(collection_data)

        names = await _resolve_user_names([collection.created_by, collection.confirmed_by])
        return _to_response(collection, names)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create collection: {str(e)}"
        )


@router.get("/summary", response_model=OutletCollectionSummaryResponse)
async def get_collection_summary(
    outlet_id: Optional[str] = Query(None, description="Filter by outlet (scoped callers see only their outlet)"),
    date_from: Optional[date] = Query(None, description="Filter delivered orders from this date (actual_delivery_date)"),
    date_to: Optional[date] = Query(None, description="Filter delivered orders up to this date (actual_delivery_date)"),
    ctx: AuthContext = Depends(require_permission(Permission.COLLECTIONS_READ)),
):
    """
    Collection summary for an outlet.

    Returns three figures:

    - **to_be_collected**: sum of `total_amount` from DELIVERED orders.
      When `date_from`/`date_to` are supplied, only orders whose
      `actual_delivery_date` falls in that window are counted.

    - **confirmed_collections**: sum of ALL CONFIRMED outlet-collection
      records for the outlet — always all-time, no date filter.
      Outstanding is a running balance, so the full history must be used.

    - **outstanding**: all-time `to_be_collected` (no date filter) minus
      all-time `confirmed_collections`.  This is always the true current
      balance regardless of any date filter supplied.

    Access: holders of collections:read. Scoped callers (e.g. OUTLET_MANAGER) see
    their own outlet; finance/admin (global) see all (or a chosen outlet).
    """
    try:
        # Scoped callers are pinned to their outlet; global callers may pick one.
        scope_ids = await _collection_scope_ids(ctx)
        if scope_ids is None:
            resolved_outlet_id = outlet_id  # all outlets (or chosen one)
        else:
            scoped_outlet = scope_ids[0] if scope_ids else None
            if not scoped_outlet or scoped_outlet == "__none__":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not assigned to any outlet"
                )
            resolved_outlet_id = scoped_outlet

        # ------------------------------------------------------------------ #
        # Fetch ALL delivered orders for this outlet (once)                   #
        # ------------------------------------------------------------------ #
        order_filters = {"order_status": OrderStatus.DELIVERED}
        if resolved_outlet_id:
            order_filters["assigned_outlet_id"] = resolved_outlet_id

        all_delivered = await order_manager.fetch_all(
            filters=order_filters,
            limit=0  # fetch all
        )

        # ------------------------------------------------------------------ #
        # 1. TO-BE-COLLECTED (period)                                         #
        #    Sum of total_amount from DELIVERED orders in the requested       #
        #    date window (actual_delivery_date).  When no date filter is      #
        #    supplied this equals the all-time total.                         #
        # ------------------------------------------------------------------ #
        to_be_collected = Decimal("0.00")
        for order in all_delivered.items:
            if date_from or date_to:
                if not order.actual_delivery_date:
                    continue
                delivery_date = ensure_date(order.actual_delivery_date)
                if date_from and delivery_date < date_from:
                    continue
                if date_to and delivery_date > date_to:
                    continue
            to_be_collected += Decimal(str(order.total_amount or 0))

        # ------------------------------------------------------------------ #
        # 2. TO-BE-COLLECTED (all-time) — needed for outstanding              #
        # ------------------------------------------------------------------ #
        to_be_collected_alltime = sum(
            Decimal(str(o.total_amount or 0)) for o in all_delivered.items
        )

        # ------------------------------------------------------------------ #
        # 3. CONFIRMED COLLECTIONS (always all-time)                          #
        #    Outstanding is a running balance; applying a date filter here    #
        #    would ignore older uncleared remittances and give a wrong total. #
        # ------------------------------------------------------------------ #
        collection_filters = {
            "confirmation_status": OutletCollectionStatus.CONFIRMED
        }
        if resolved_outlet_id:
            collection_filters["outlet_id"] = resolved_outlet_id

        all_confirmed = await collection_manager.fetch_all(
            filters=collection_filters,
            limit=0  # fetch all
        )

        confirmed_collections = sum(
            Decimal(str(col.amount or 0)) for col in all_confirmed.items
        )

        # ------------------------------------------------------------------ #
        # 4. OUTSTANDING (always all-time)                                    #
        #    = all-time deliveries − all-time confirmed remittances           #
        # ------------------------------------------------------------------ #
        outstanding = to_be_collected_alltime - confirmed_collections

        return OutletCollectionSummaryResponse(
            outlet_id=resolved_outlet_id,
            date_from=date_from,
            date_to=date_to,
            to_be_collected=to_be_collected,          # period figure (date-filtered)
            confirmed_collections=confirmed_collections,  # all-time
            outstanding=outstanding                   # all-time true balance
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to compute collection summary: {str(e)}"
        )


@router.get("", response_model=ListResponse[OutletCollectionResponse])
async def list_collections(
    outlet_id: Optional[str] = Query(None, description="Filter by outlet"),
    date_from: Optional[date] = Query(None, description="Filter from date"),
    date_to: Optional[date] = Query(None, description="Filter to date"),
    confirmation_status: Optional[OutletCollectionStatus] = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=0, le=500),
    offset: int = Query(0, ge=0),
    ctx: AuthContext = Depends(require_permission(Permission.COLLECTIONS_READ)),
):
    """
    List outlet collections with filters.

    Row scope via apply_scope: outlet managers see their outlet, cluster/state
    heads their outlets, finance/admin global. A scoped role can't widen via the
    outlet_id query param (apply_scope overrides it).
    """
    try:
        # Build filters
        filters = {}
        if outlet_id:
            filters["outlet_id"] = outlet_id
        if confirmation_status:
            filters["confirmation_status"] = confirmation_status

        filters = await apply_scope(filters, ctx)

        # Note: Date filtering has issues with SharedBackend's filter syntax
        # For now, we'll fetch all and filter in Python if date filters are provided
        # This is a temporary workaround until SharedBackend filter is fixed
        
        # Fetch collections
        collections = await collection_manager.fetch_all(
            limit=limit if not (date_from or date_to) else 0,  # Fetch all if date filtering
            offset=offset if not (date_from or date_to) else 0,
            filters=filters if filters else None,
            sorts=["-date", "-created_at"]
        )
        
        # Apply date filtering in Python if needed
        filtered_items = collections.items
        if date_from or date_to:
            filtered_items = [
                c for c in collections.items
                if (not date_from or c.date >= date_from) and
                   (not date_to or c.date <= date_to)
            ]
            # Apply pagination after filtering
            filtered_items = filtered_items[offset:offset + limit] if limit > 0 else filtered_items
        
        # Resolve actor names once for the whole page (bounded: one outlet, <=500 rows)
        names = await _resolve_user_names(
            [c.created_by for c in filtered_items]
            + [c.confirmed_by for c in filtered_items]
        )
        items = [_to_response(c, names) for c in filtered_items]

        return ListResponse(items=items, count=len(filtered_items))
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch collections: {str(e)}"
        )


@router.get("/{collection_id}", response_model=OutletCollectionResponse)
async def get_collection(
    collection_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.COLLECTIONS_READ)),
):
    """
    Get a single collection record

    Access: holders of collections:read; scoped callers limited to their outlet.
    """
    try:
        collection = await collection_manager.fetch(collection_id)

        await _assert_collection_in_scope(ctx, collection)

        names = await _resolve_user_names([collection.created_by, collection.confirmed_by])
        return _to_response(collection, names)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection not found: {str(e)}"
        )


@router.patch("/{collection_id}/status", response_model=OutletCollectionResponse)
async def update_collection_status(
    collection_id: str,
    payload: OutletCollectionStatusUpdateRequest,
    ctx: AuthContext = Depends(require_permission(Permission.COLLECTIONS_WRITE)),
):
    """
    Update collection confirmation status

    Status transitions:
    - PENDING -> CONFIRMED
    - PENDING -> NOT_RECEIVED
    - NOT_RECEIVED -> CONFIRMED

    Access: collections:write at GLOBAL scope (finance/admin checker tier).
    OUTLET_MANAGER (records collections) cannot confirm/reject.
    """
    try:
        # Confirming/rejecting is a checker action — finance/admin (global) only.
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Confirming/deleting collections requires finance/admin (global) scope"
            )

        # Fetch current collection
        collection = await collection_manager.fetch(collection_id)
        
        # Validate status transition
        current_status = collection.confirmation_status
        new_status = payload.confirmation_status
        
        valid_transitions = {
            OutletCollectionStatus.PENDING: [
                OutletCollectionStatus.CONFIRMED,
                OutletCollectionStatus.NOT_RECEIVED
            ],
            OutletCollectionStatus.NOT_RECEIVED: [
                OutletCollectionStatus.CONFIRMED
            ],
            OutletCollectionStatus.CONFIRMED: []  # Cannot change from CONFIRMED
        }
        
        if new_status not in valid_transitions.get(current_status, []):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status transition: {current_status.value} -> {new_status.value}"
            )
        
        # Prepare updates
        updates = {
            "confirmation_status": new_status
        }
        
        # Set confirmed_by and confirmed_at when status becomes CONFIRMED
        if new_status == OutletCollectionStatus.CONFIRMED:
            updates["confirmed_by"] = ctx.user_id
            updates["confirmed_at"] = datetime.utcnow()
        
        # Update collection
        await collection_manager.update(
            collection_id,
            updates
        )
        
        # Fetch fresh copy to avoid session detachment issues
        updated_collection = await collection_manager.fetch(collection_id)

        names = await _resolve_user_names(
            [updated_collection.created_by, updated_collection.confirmed_by]
        )
        return _to_response(updated_collection, names)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update collection status: {str(e)}"
        )


@router.delete("/{collection_id}", response_model=StatusResponse)
async def delete_collection(
    collection_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.COLLECTIONS_WRITE)),
):
    """
    Delete a collection record

    Use this if a collection was created by mistake.

    Access: collections:write at GLOBAL scope (finance/admin checker tier).
    OUTLET_MANAGER cannot delete.
    """
    try:
        # Deleting is a checker action — finance/admin (global) only.
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Confirming/deleting collections requires finance/admin (global) scope"
            )

        # Fetch collection to get details for response
        collection = await collection_manager.fetch(collection_id)
        
        # Delete using direct session delete (to avoid SharedBackend bug)
        async with collection_manager.session_factory() as session:
            import sqlalchemy as db
            query = db.select(OutletDailyCollectionSchema).filter_by(uid=collection_id)
            result = await session.execute(query)
            collection_to_delete = result.scalar_one_or_none()
            
            if not collection_to_delete:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Collection not found"
                )
            
            await session.delete(collection_to_delete)
            await session.commit()
        
        return StatusResponse(
            status="ok",
            message=f"Collection record for {collection.date} deleted successfully"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete collection: {str(e)}"
        )
