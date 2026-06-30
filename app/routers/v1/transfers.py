from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional
from datetime import datetime, date

from config import get_settings, get_engine
from managers import (
    StockTransferOrderManager, TransferItemManager, InventoryManager,
    ProductManager, OutletManager, UserManager,
    StockTransferOrderSchema, TransferItemSchema, InventorySchema
)
from models import (
    StockTransferCreateRequest, StockTransferStatusUpdateRequest,
    StockTransferApproveQuantitiesRequest,
    StockTransferResponse, TransferItemResponse, ProductBrief, OutletBrief, UserBrief,
    ListResponse, StatusResponse , BulkTransferResponse , StockTransferCreateRequestBulk
)
from utils.auth import require_permission, apply_scope, AuthContext
from utils.permissions import Permission, ScopeLevel
from utils.constants import TransferStatus, OutletType
from utils.warehouse_utils import get_default_warehouse_id
import uuid

settings = get_settings()
engine = get_engine(settings.name)

transfer_manager = StockTransferOrderManager(engine)
transfer_item_manager = TransferItemManager(engine)
inventory_manager = InventoryManager(engine)
product_manager = ProductManager(engine)
outlet_manager = OutletManager(engine)
user_manager = UserManager(engine)

router = APIRouter(prefix="/transfers", tags=["Stock Transfer Management"])


def generate_transfer_number() -> str:
    """Generate unique transfer number"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"TRF-{timestamp}-{str(uuid.uuid4())[:8].upper()}"


async def _transfer_scope_ids(ctx: AuthContext):
    """Outlet-ids the caller may touch, or None for unrestricted (GLOBAL/microservice).

    Transfers carry two outlet FKs (from/to), so a scoped caller is matched by an OR
    over both. GLOBAL roles (admin, warehouse, ops admin, super) get None = no narrowing
    (warehouse fulfils network-wide — decision 2026-06-29). OUTLET -> [own]; CLUSTER/STATE
    -> their covered outlets; anything scoped without an outlet dimension -> deny.
    """
    if ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value:
        return None
    sv = (await apply_scope({}, ctx)).get("outlet_id")
    if sv is None:
        return ["__none__"]  # scoped but no outlet scope (e.g. agency) -> match nothing
    return sv if isinstance(sv, list) else [sv]


async def _assert_transfer_in_scope(ctx: AuthContext, transfer):
    """Scoped (non-global) writers may only act on transfers touching their outlet(s)."""
    scope_ids = await _transfer_scope_ids(ctx)
    if scope_ids is None:
        return
    if transfer.from_outlet_id in scope_ids or transfer.to_outlet_id in scope_ids:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access denied: this transfer does not involve an outlet in your scope",
    )


async def _fetch_transfers_scoped(ctx: AuthContext, base_filters: dict, *,
                                  from_outlet_id=None, to_outlet_id=None, limit=50, offset=0):
    """Fetch transfers within the caller's scope, returning (items, total).

    GLOBAL -> straight filtered query. Scoped -> two queries (incoming `to ∈ scope`,
    outgoing `from ∈ scope`), deduped/sorted in memory then paginated — mirrors the old
    outlet-manager path, generalised from a single outlet id to the caller's outlet set.
    ponytail: the scoped `count` reflects the capped pre-fetch, same as the prior code.
    """
    scope_ids = await _transfer_scope_ids(ctx)
    if scope_ids is None:
        filters = dict(base_filters)
        if from_outlet_id is not None:
            filters["from_outlet_id"] = from_outlet_id
        if to_outlet_id is not None:
            filters["to_outlet_id"] = to_outlet_id
        res = await transfer_manager.fetch_all(filters=filters, limit=limit, offset=offset, sorts=["-created_at"])
        return res.items, res.count

    prefetch = max(limit + offset, 100)
    items = []
    # Incoming: transfers landing in one of the caller's outlets
    if to_outlet_id is None or to_outlet_id in scope_ids:
        f = dict(base_filters)
        f["to_outlet_id"] = scope_ids
        if from_outlet_id is not None:
            f["from_outlet_id"] = from_outlet_id
        items += (await transfer_manager.fetch_all(filters=f, limit=prefetch, sorts=["-created_at"])).items
    # Outgoing: transfers leaving one of the caller's outlets
    if from_outlet_id is None or from_outlet_id in scope_ids:
        f = dict(base_filters)
        f["from_outlet_id"] = scope_ids
        if to_outlet_id is not None:
            f["to_outlet_id"] = to_outlet_id
        items += (await transfer_manager.fetch_all(filters=f, limit=prefetch, sorts=["-created_at"])).items

    unique = {t.uid: t for t in items}
    ordered = sorted(unique.values(), key=lambda x: x.created_at, reverse=True)
    return ordered[offset:offset + limit], len(ordered)


@router.post("", response_model=StockTransferResponse)
async def create_transfer_request(
    payload: StockTransferCreateRequest,
    ctx: AuthContext = Depends(require_permission(Permission.TRANSFERS_WRITE)),
):
    """
    Create stock transfer request
    - Scoped managers (outlet/cluster/state) can only create transfers touching their outlets
    - Warehouse/ops managers and admins (GLOBAL) can create any transfer
    """
    try:
        current_user_id = ctx.user_id

        # Source and destination are both mandatory and must differ
        if not payload.from_outlet_id or not payload.to_outlet_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Both source and destination outlets are required."
            )
        if payload.from_outlet_id == payload.to_outlet_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Source and destination outlets must be different."
            )

        # Validate source and destination
        if payload.from_outlet_id:
            try:
                from_outlet = await outlet_manager.fetch(payload.from_outlet_id)
                if not from_outlet.is_active:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Source outlet is not active"
                    )
            except:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Source outlet not found"
                )
        
        # Validate destination outlet
        try:
            to_outlet = await outlet_manager.fetch(payload.to_outlet_id)
            if not to_outlet.is_active:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Destination outlet is not active"
                )
        except:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Destination outlet not found"
            )
        
        # Scope fence: a scoped caller's outlet(s) must be the source or destination.
        scope_ids = await _transfer_scope_ids(ctx)
        if scope_ids is not None and \
                payload.from_outlet_id not in scope_ids and payload.to_outlet_id not in scope_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only create transfers where one of your outlets is the source or destination"
            )
        
        # Validate products and check availability
        validated_items = []
        for item in payload.items:
            # Verify product exists
            try:
                product = await product_manager.fetch(item.product_id)
                if not product.is_active:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Product {product.product_name} is not active"
                    )
            except:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product not found: {item.product_id}"
                )
            
            # Skip stock check if source is a factory (infinite supply)
            source_is_factory = False
            if payload.from_outlet_id:
                try:
                    src = await outlet_manager.fetch(payload.from_outlet_id)
                    source_is_factory = src.outlet_type == OutletType.FACTORY
                except Exception:
                    pass

            if not source_is_factory:
                warehouse_id = await get_default_warehouse_id(engine)
                source_outlet_id = payload.from_outlet_id if payload.from_outlet_id else warehouse_id
                source_inventory = await inventory_manager.fetch_all(
                    filters={
                        "product_id": item.product_id,
                        "outlet_id": source_outlet_id
                    }
                )

                if not source_inventory.items:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"No stock available for {product.product_name} at source location"
                    )

                inventory_item = source_inventory.items[0]
                available_stock = inventory_item.quantity

                if available_stock < item.quantity_requested:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Insufficient stock for {product.product_name}. Available: {available_stock}, Requested: {item.quantity_requested}"
                    )
            
            validated_items.append({
                "product": product,
                "quantity_requested": item.quantity_requested
            })
        
        # Create transfer order
        new_transfer = StockTransferOrderSchema(
            transfer_number=generate_transfer_number(),
            from_outlet_id=payload.from_outlet_id,
            to_outlet_id=payload.to_outlet_id,
            status=TransferStatus.PENDING,
            requested_by=current_user_id,
            scheduled_date=payload.scheduled_date,
            notes=payload.notes
        )
        
        created_transfer = await transfer_manager.create(new_transfer)
        
        # Create transfer items
        for item_data in validated_items:
            transfer_item = TransferItemSchema(
                transfer_id=created_transfer.uid,
                product_id=item_data["product"].uid,
                quantity_requested=item_data["quantity_requested"],
                quantity_delivered=0
            )
            await transfer_item_manager.create(transfer_item)
        
        return await get_transfer_response(created_transfer.uid)
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create transfer request: {str(e)}"
        )
@router.post("/mass-upload", response_model=BulkTransferResponse)
async def mass_upload_transfer_requests(
    payload: List[StockTransferCreateRequestBulk],
    ctx: AuthContext = Depends(require_permission(Permission.TRANSFERS_WRITE)),
):
    """
    Mass upload stock transfer requests.
    - Bulk admin tooling: restricted to GLOBAL-scope writers (admin/warehouse/ops/super).
    - Processes the batch and returns a summary of successes and failures.
    """
    # Bulk create by outlet *name* can't be safely scope-fenced per row; keep it global-only
    # (matches the prior admin/warehouse audience) so scoped writers can't bulk-escape scope.
    if await _transfer_scope_ids(ctx) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Mass upload is restricted to organisation-wide (global) roles"
        )
    current_user_id = ctx.user_id

    successful_transfers = []
    errors = []

    for index, transfer_req in enumerate(payload):
        try:
            # 1. Validate Source Outlet (if provided)
            from_outlet_id = None
            if transfer_req.from_outlet_name:
                try:
                    from_outlets = await outlet_manager.fetch_all(filters={"outlet_name": transfer_req.from_outlet_name})
                    if not from_outlets.items:
                        raise ValueError(f"Source outlet '{transfer_req.from_outlet_name}' not found")
                    from_outlet = from_outlets.items[0]
                    if not from_outlet.is_active:
                        raise ValueError(f"Source outlet '{transfer_req.from_outlet_name}' is not active")
                    from_outlet_id = from_outlet.uid
                except Exception as e:
                    raise ValueError(str(e))
            
            # 2. Validate Destination Outlet
            try:
                to_outlets = await outlet_manager.fetch_all(filters={"outlet_name": transfer_req.to_outlet_name})
                if not to_outlets.items:
                    raise ValueError(f"Destination outlet '{transfer_req.to_outlet_name}' not found")
                to_outlet = to_outlets.items[0]
                if not to_outlet.is_active:
                    raise ValueError(f"Destination outlet '{transfer_req.to_outlet_name}' is not active")
                to_outlet_id = to_outlet.uid
            except Exception as e:
                raise ValueError(str(e))

            # Source is mandatory and must differ from destination
            if not from_outlet_id:
                raise ValueError("Source outlet is required")
            if from_outlet_id == to_outlet_id:
                raise ValueError("Source and destination outlets must be different")

            # 3. Validate Products and Check Availability
            validated_items = []
            for item in transfer_req.items:
                # Verify product exists by SKU
                try:
                    product_search = await product_manager.fetch_all(filters={"sku": item.sku})
                    if not product_search.items:
                        raise ValueError(f"Product with SKU '{item.sku}' not found")
                    product = product_search.items[0]
                    if not product.is_active:
                        raise ValueError(f"Product {product.product_name} (SKU: {item.sku}) is not active")
                except Exception as e:
                    raise ValueError(str(e))
                
                # Skip stock check if source is a factory (infinite supply)
                bulk_source_is_factory = False
                if from_outlet_id:
                    try:
                        src = await outlet_manager.fetch(from_outlet_id)
                        bulk_source_is_factory = src.outlet_type == OutletType.FACTORY
                    except Exception:
                        pass

                if not bulk_source_is_factory:
                    warehouse_id = await get_default_warehouse_id(engine)
                    source_outlet_id = from_outlet_id if from_outlet_id else warehouse_id
                    source_inventory = await inventory_manager.fetch_all(
                        filters={
                            "product_id": product.uid,
                            "outlet_id": source_outlet_id
                        }
                    )

                    if not source_inventory.items:
                        raise ValueError(f"No stock available for {product.product_name} at source location")

                    inventory_item = source_inventory.items[0]
                    available_stock = inventory_item.quantity

                    if available_stock < item.quantity_requested:
                        raise ValueError(
                            f"Insufficient stock for {product.product_name}. "
                            f"Available: {available_stock}, Requested: {item.quantity_requested}"
                        )
                
                validated_items.append({
                    "product": product,
                    "quantity_requested": item.quantity_requested
                })

            # 4. Create Transfer Order
            new_transfer = StockTransferOrderSchema(
                transfer_number=generate_transfer_number(),
                from_outlet_id=from_outlet_id,
                to_outlet_id=to_outlet_id,
                status=TransferStatus.PENDING,
                requested_by=current_user_id,
                scheduled_date=transfer_req.scheduled_date,
                notes=transfer_req.notes
            )
            
            created_transfer = await transfer_manager.create(new_transfer)
            
            # 5. Create Transfer Items
            for item_data in validated_items:
                transfer_item = TransferItemSchema(
                    transfer_id=created_transfer.uid,
                    product_id=item_data["product"].uid,
                    quantity_requested=item_data["quantity_requested"],
                    quantity_delivered=0
                )
                await transfer_item_manager.create(transfer_item)
            
            successful_transfers.append(created_transfer.uid)

        except Exception as batch_error:
            # Catch errors for this specific row so the rest of the batch can continue
            errors.append({
                "to_outlet_name": transfer_req.to_outlet_name,
                "error_detail": str(batch_error)
            })

    # Return the summary report
    return BulkTransferResponse(
        successful_count=len(successful_transfers),
        failed_count=len(errors),
        successful_transfer_ids=successful_transfers,
        errors=errors
    )

@router.get("/test-deployment")
async def test_deployment():
    """Test endpoint to verify deployment """
    return {"message": "Code updated successfully", "timestamp": datetime.now()}


async def get_transfer_responses_batch(transfers: List[StockTransferOrderSchema]) -> List[StockTransferResponse]:
    """Helper to build complete transfer responses in batch to prevent N+1 database queries."""
    if not transfers:
        return []

    # 1. Collect all IDs
    to_outlet_ids = list({t.to_outlet_id for t in transfers if t.to_outlet_id})
    from_outlet_ids = list({t.from_outlet_id for t in transfers if t.from_outlet_id})
    all_outlet_ids = list(set(to_outlet_ids + from_outlet_ids))

    user_ids = list(
        {t.requested_by for t in transfers if t.requested_by} |
        {t.approved_by for t in transfers if t.approved_by} |
        {t.delivery_person_id for t in transfers if t.delivery_person_id}
    )

    transfer_ids = [t.uid for t in transfers]

    # 2. Fetch outlets, users, and transfer items in parallel/batch
    outlets_map = {}
    users_map = {}
    items_by_transfer = {}
    products_map = {}

    if all_outlet_ids:
        try:
            outlets_res = await outlet_manager.fetch_all(filters={"uid": all_outlet_ids}, limit=len(all_outlet_ids))
            outlets_map = {o.uid: o for o in outlets_res.items}
        except Exception as e:
            print(f"Error fetching outlets batch: {str(e)}")

    if user_ids:
        try:
            users_res = await user_manager.fetch_all(filters={"uid": user_ids}, limit=len(user_ids))
            users_map = {u.uid: u for u in users_res.items}
        except Exception as e:
            print(f"Error fetching users batch: {str(e)}")

    # Fetch all items for all transfers in one query
    all_items = []
    if transfer_ids:
        try:
            items_res = await transfer_item_manager.fetch_all(
                filters={"transfer_id": transfer_ids},
                limit=10000  # Large enough limit to fetch all items at once
            )
            all_items = items_res.items
            for item in all_items:
                if item.transfer_id not in items_by_transfer:
                    items_by_transfer[item.transfer_id] = []
                items_by_transfer[item.transfer_id].append(item)
        except Exception as e:
            print(f"Error fetching transfer items batch: {str(e)}")

    # Fetch all products for these items in one query
    product_ids = list({item.product_id for item in all_items if item.product_id})
    if product_ids:
        try:
            products_res = await product_manager.fetch_all(filters={"uid": product_ids}, limit=len(product_ids))
            products_map = {p.uid: p for p in products_res.items}
        except Exception as e:
            print(f"Error fetching products batch: {str(e)}")

    # 3. Assemble responses
    responses = []
    for transfer in transfers:
        # Nested: source outlet
        from_outlet = None
        if transfer.from_outlet_id and transfer.from_outlet_id in outlets_map:
            outlet = outlets_map[transfer.from_outlet_id]
            from_outlet = OutletBrief(
                uid=outlet.uid,
                outlet_name=outlet.outlet_name,
                outlet_code=outlet.outlet_code,
            )

        # Nested: destination outlet
        to_outlet = None
        if transfer.to_outlet_id and transfer.to_outlet_id in outlets_map:
            outlet = outlets_map[transfer.to_outlet_id]
            to_outlet = OutletBrief(
                uid=outlet.uid,
                outlet_name=outlet.outlet_name,
                outlet_code=outlet.outlet_code,
            )

        # Nested: requesting user
        requested_by_user = None
        if transfer.requested_by and transfer.requested_by in users_map:
            requester = users_map[transfer.requested_by]
            requested_by_user = UserBrief(
                uid=requester.uid,
                full_name=requester.full_name,
                email=requester.email,
                role=requester.role.value if hasattr(requester.role, "value") else str(requester.role),
            )

        # Assemble transfer items
        transfer_items = items_by_transfer.get(transfer.uid, [])
        items = []
        for item in transfer_items:
            product_brief = None
            if item.product_id and item.product_id in products_map:
                product = products_map[item.product_id]
                product_brief = ProductBrief(
                    uid=product.uid,
                    product_name=product.product_name,
                    sku=product.sku,
                )
            items.append(TransferItemResponse(
                uid=item.uid,
                product_id=item.product_id,
                product=product_brief,
                quantity_requested=item.quantity_requested,
                quantity_delivered=item.quantity_delivered,
            ))

        responses.append(StockTransferResponse(
            uid=transfer.uid,
            transfer_number=transfer.transfer_number,
            from_outlet_id=transfer.from_outlet_id,
            from_outlet=from_outlet,
            to_outlet_id=transfer.to_outlet_id,
            to_outlet=to_outlet,
            status=transfer.status,
            requested_by=transfer.requested_by,
            requested_by_user=requested_by_user,
            approved_by=transfer.approved_by,
            delivery_person_id=transfer.delivery_person_id,
            scheduled_date=transfer.scheduled_date,
            delivered_date=transfer.delivered_date,
            notes=transfer.notes,
            items=items,
            created_at=transfer.created_at,
        ))

    return responses


@router.get("/pending-approvals", response_model=ListResponse[StockTransferResponse])
async def get_pending_approvals(
    limit: int = 50,
    offset: int = 0,
    ctx: AuthContext = Depends(require_permission(Permission.TRANSFERS_READ)),
):
    """Get transfers pending approval (scoped to the caller's outlets; GLOBAL = all)."""
    try:
        items, total = await _fetch_transfers_scoped(
            ctx, {"status": TransferStatus.PENDING}, limit=limit, offset=offset
        )
        transfer_responses = await get_transfer_responses_batch(items)
        return ListResponse(items=transfer_responses, count=total)

    except Exception as e:
        error_msg = str(e)
        if "record not found" in error_msg.lower():
            return ListResponse(items=[], count=0)
        
        print(f"Error in pending approvals: {error_msg}")
        return ListResponse(items=[], count=0)


# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

@router.get("/{transfer_id}", response_model=StockTransferResponse)
async def get_transfer(
    transfer_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.TRANSFERS_READ)),
):
    """Get specific transfer details"""
    try:
        transfer = await transfer_manager.fetch(transfer_id)

        # Scope: a scoped caller may only view transfers touching their outlet(s).
        await _assert_transfer_in_scope(ctx, transfer)

        # Get transfer items
        transfer_items = await transfer_item_manager.fetch_all(
            filters={"transfer_id": transfer_id}
        )
        
        # Build response with items
        items = []
        for item in transfer_items.items:
            try:
                product = await product_manager.fetch(item.product_id)
                items.append(TransferItemResponse(
                    uid=item.uid,
                    product_id=item.product_id,
                    product_name=product.product_name,
                    requested_quantity=item.requested_quantity,
                    approved_quantity=item.approved_quantity,
                    notes=item.notes
                ))
            except:
                items.append(TransferItemResponse(
                    uid=item.uid,
                    product_id=item.product_id,
                    product_name="Unknown Product",
                    requested_quantity=item.requested_quantity,
                    approved_quantity=item.approved_quantity,
                    notes=item.notes
                ))
        
        return StockTransferResponse(
            uid=transfer.uid,
            transfer_number=transfer.transfer_number,
            from_outlet_id=transfer.from_outlet_id,
            to_outlet_id=transfer.to_outlet_id,
            status=transfer.status,
            requested_by=transfer.requested_by,
            approved_by=transfer.approved_by,
            notes=transfer.notes,
            items=items,
            created_at=transfer.created_at,
            last_updated=transfer.last_updated
        )
    
    except HTTPException:
        raise
    except Exception as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transfer not found"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch transfer: {str(e)}"
        )


@router.get("/reports/transfer-summary")
async def get_transfer_summary(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    outlet_id: Optional[str] = None,
    ctx: AuthContext = Depends(require_permission(Permission.TRANSFERS_READ)),
):
    """Get transfer summary report (network-wide; global roles only for now)."""
    # This aggregate spans all outlets; a per-scope summary is a follow-up, so keep it to
    # GLOBAL readers (the prior warehouse/admin audience) rather than leak totals. Raised
    # before the try below since that try re-wraps everything as a 500.
    if await _transfer_scope_ids(ctx) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Transfer summary is available to organisation-wide (global) roles only"
        )
    try:
        # Build filters
        filters = {}
        
        if from_date:
            filters["created_at__gte"] = from_date
        if to_date:
            filters["created_at__lte"] = to_date
        if outlet_id:
            filters["from_outlet_id"] = outlet_id
        
        # Get transfers
        transfers = await transfer_manager.fetch_all(filters=filters)
        
        # Calculate summary statistics
        total_transfers = len(transfers.items)
        status_breakdown = {}
        outlet_breakdown = {}
        
        for transfer in transfers.items:
            # Status breakdown
            status = transfer.status.value
            status_breakdown[status] = status_breakdown.get(status, 0) + 1
            
            # Outlet breakdown
            from_outlet = transfer.from_outlet_id or "Warehouse"
            to_outlet = transfer.to_outlet_id or "Warehouse"
            
            if from_outlet not in outlet_breakdown:
                outlet_breakdown[from_outlet] = {"outgoing": 0, "incoming": 0}
            if to_outlet not in outlet_breakdown:
                outlet_breakdown[to_outlet] = {"outgoing": 0, "incoming": 0}
            
            outlet_breakdown[from_outlet]["outgoing"] += 1
            outlet_breakdown[to_outlet]["incoming"] += 1
        
        return {
            "period": {
                "from_date": from_date.isoformat() if from_date else None,
                "to_date": to_date.isoformat() if to_date else None
            },
            "summary": {
                "total_transfers": total_transfers,
                "status_breakdown": status_breakdown,
                "outlet_breakdown": outlet_breakdown
            }
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate transfer summary: {str(e)}"
        )




@router.put("/{transfer_id}/approve", response_model=StockTransferResponse)
async def approve_transfer(
    transfer_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.TRANSFERS_WRITE)),
):
    """Approve stock transfer request"""
    try:
        current_user_id = ctx.user_id
        # Get transfer
        transfer = await transfer_manager.fetch(transfer_id)

        # Scope fence: scoped writers may only approve transfers touching their outlet(s).
        await _assert_transfer_in_scope(ctx, transfer)

        if transfer.status != TransferStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only pending transfers can be approved"
            )
        
        # Update transfer status
        updates = {
            "status": TransferStatus.APPROVED,
            "approved_by": current_user_id
        }
        
        updated_transfer = await transfer_manager.update(transfer_id, updates)
        return updated_transfer
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to approve transfer: {str(e)}"
        )


@router.put("/{transfer_id}/status", response_model=StatusResponse)
async def update_transfer_status(
    transfer_id: str,
    payload: StockTransferStatusUpdateRequest,
    ctx: AuthContext = Depends(require_permission(Permission.TRANSFERS_WRITE)),
):
    """Update transfer status"""
    try:
        current_user_id = ctx.user_id

        # Get current transfer to validate transition
        transfer = await transfer_manager.fetch(transfer_id)

        # Scope fence: scoped writers may only touch transfers involving their outlet(s).
        await _assert_transfer_in_scope(ctx, transfer)

        # Validate status transition
        if not is_valid_transfer_status_transition(transfer.status, payload.status):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status transition from {transfer.status} to {payload.status}"
            )

        updates = {
            "status": payload.status,
            "notes": payload.notes
        }

        # Handle status-specific logic (no reservation)
        if payload.status == TransferStatus.APPROVED:
            updates["approved_by"] = current_user_id

        elif payload.status == TransferStatus.IN_TRANSIT:
            # Deduct stock from source when moving to IN_TRANSIT
            await deduct_stock_from_source(transfer_id)

        elif payload.status == TransferStatus.DELIVERED:
            from datetime import datetime
            updates["delivered_date"] = datetime.utcnow()
            # Complete the stock transfer by adding stock to destination
            await add_stock_to_destination(transfer_id)

        elif payload.status == TransferStatus.CANCELLED:
            # Revert stock deduction if transfer is cancelled after being in transit
            if transfer.status == TransferStatus.IN_TRANSIT:
                await revert_stock_to_source(transfer_id)
        
        await transfer_manager.update(transfer_id, updates)
        
        return StatusResponse(
            status="ok",
            message=f"Transfer status updated to {payload.status}"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update transfer status: {str(e)}"
        )


# GENERIC ROUTE LAST (after all specific routes)

@router.get("", response_model=ListResponse[StockTransferResponse])
async def get_transfers(
    transfer_status: Optional[TransferStatus] = None,
    from_outlet_id: Optional[str] = None,
    to_outlet_id: Optional[str] = None,
    requested_by: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    limit: int = 50,
    offset: int = 0,
    ctx: AuthContext = Depends(require_permission(Permission.TRANSFERS_READ)),
):
    """
    Get transfer requests with filters.

    Scope is applied as an OR over the transfer's two outlet FKs: a scoped caller
    (outlet/cluster/state) sees only transfers where one of their outlets is the source
    or destination; GLOBAL roles (admin/warehouse/ops/super) see everything.
    """
    try:
        # Filters common to both the global and scoped paths.
        base_filters = {}
        if transfer_status:
            base_filters["status"] = transfer_status
        if requested_by:
            base_filters["requested_by"] = requested_by
        if from_date or to_date:
            from datetime import datetime, time
            base_filters["created_at"] = {}
            if from_date:
                base_filters["created_at"][">="] = datetime.combine(from_date, time.min)
            if to_date:
                base_filters["created_at"]["<="] = datetime.combine(to_date, time.max)

        items, total = await _fetch_transfers_scoped(
            ctx, base_filters,
            from_outlet_id=from_outlet_id, to_outlet_id=to_outlet_id,
            limit=limit, offset=offset,
        )

        # Build responses in batch to avoid slow N+1 queries
        transfer_responses = await get_transfer_responses_batch(items)

        return ListResponse(items=transfer_responses, count=total)

    except Exception as e:
        # Handle "record not found" errors gracefully
        error_msg = str(e)
        if "record not found" in error_msg.lower():
            return ListResponse(items=[], count=0)
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch transfers: {error_msg}"
        )


@router.put("/{transfer_id}/approve-with-quantities", response_model=StatusResponse)
async def approve_transfer_with_quantities(
    transfer_id: str,
    payload: StockTransferApproveQuantitiesRequest,
    ctx: AuthContext = Depends(require_permission(Permission.TRANSFERS_WRITE)),
):
    """
    Approve transfer request and potentially modify requested quantities.
    Scoped writers may only approve transfers touching their outlet(s); GLOBAL roles any.
    """
    try:
        current_user_id = ctx.user_id
        transfer = await transfer_manager.fetch(transfer_id)

        # Scope fence: scoped writers limited to transfers involving their outlet(s).
        await _assert_transfer_in_scope(ctx, transfer)

        # Validations
        if transfer.status != TransferStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Only PENDING transfers can be approved. Current status: {transfer.status}"
            )

        # Map item modifications
        items_dict = {item.product_id: item.approved_quantity for item in payload.items}
        
        # Update quantities
        import sqlalchemy as db
        async with transfer_manager.session_factory() as session:
            for product_id, approved_qty in items_dict.items():
                await session.execute(
                    db.update(TransferItemSchema)
                    .where(
                        db.and_(
                            TransferItemSchema.transfer_id == transfer_id,
                            TransferItemSchema.product_id == product_id
                        )
                    )
                    .values(quantity_requested=approved_qty)
                )
            
            await session.execute(
                db.update(StockTransferOrderSchema)
                .where(StockTransferOrderSchema.uid == transfer_id)
                .values(
                    status=TransferStatus.APPROVED,
                    approved_by=current_user_id,
                    notes=db.case((payload.notes != None, payload.notes), else_=StockTransferOrderSchema.notes)
                )
            )
            await session.commit()
        
        return StatusResponse(
            status="ok",
            message="Transfer approved with modified quantities"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to approve transfer: {str(e)}"
        )


async def get_transfer_response(transfer_id: str) -> StockTransferResponse:
    """Helper to build complete transfer response with nested outlet, user and product details."""
    try:
        transfer = await transfer_manager.fetch(transfer_id)

        # Nested: source outlet
        from_outlet = None
        if transfer.from_outlet_id:
            try:
                outlet = await outlet_manager.fetch(transfer.from_outlet_id)
                from_outlet = OutletBrief(
                    uid=outlet.uid,
                    outlet_name=outlet.outlet_name,
                    outlet_code=outlet.outlet_code,
                )
            except Exception:
                pass

        # Nested: destination outlet
        to_outlet = None
        try:
            outlet = await outlet_manager.fetch(transfer.to_outlet_id)
            to_outlet = OutletBrief(
                uid=outlet.uid,
                outlet_name=outlet.outlet_name,
                outlet_code=outlet.outlet_code,
            )
        except Exception:
            pass

        # Nested: requesting user
        requested_by_user = None
        try:
            requester = await user_manager.fetch(transfer.requested_by)
            requested_by_user = UserBrief(
                uid=requester.uid,
                full_name=requester.full_name,
                email=requester.email,
                role=requester.role.value if hasattr(requester.role, "value") else str(requester.role),
            )
        except Exception:
            pass

        # Transfer items with nested product info
        items = []
        try:
            transfer_items = await transfer_item_manager.fetch_all(
                filters={"transfer_id": transfer_id}
            )
            for item in transfer_items.items:
                product_brief = None
                try:
                    product = await product_manager.fetch(item.product_id)
                    product_brief = ProductBrief(
                        uid=product.uid,
                        product_name=product.product_name,
                        sku=product.sku,
                    )
                except Exception:
                    pass
                items.append(TransferItemResponse(
                    uid=item.uid,
                    product_id=item.product_id,
                    product=product_brief,
                    quantity_requested=item.quantity_requested,
                    quantity_delivered=item.quantity_delivered,
                ))
        except Exception as e:
            print(f"Error fetching transfer items for {transfer_id}: {str(e)}")

        return StockTransferResponse(
            uid=transfer.uid,
            transfer_number=transfer.transfer_number,
            from_outlet_id=transfer.from_outlet_id,
            from_outlet=from_outlet,
            to_outlet_id=transfer.to_outlet_id,
            to_outlet=to_outlet,
            status=transfer.status,
            requested_by=transfer.requested_by,
            requested_by_user=requested_by_user,
            approved_by=transfer.approved_by,
            delivery_person_id=transfer.delivery_person_id,
            scheduled_date=transfer.scheduled_date,
            delivered_date=transfer.delivered_date,
            notes=transfer.notes,
            items=items,
            created_at=transfer.created_at,
        )

    except Exception as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Transfer not found: {transfer_id}"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to build transfer response: {str(e)}"
        )


def is_valid_transfer_status_transition(current_status: TransferStatus, new_status: TransferStatus) -> bool:
    """Validate transfer status transitions"""
    valid_transitions = {
        TransferStatus.PENDING: [TransferStatus.APPROVED, TransferStatus.CANCELLED],
        TransferStatus.APPROVED: [TransferStatus.IN_TRANSIT, TransferStatus.CANCELLED],
        TransferStatus.IN_TRANSIT: [TransferStatus.DELIVERED, TransferStatus.CANCELLED],
        TransferStatus.DELIVERED: [],  # Final state
        TransferStatus.CANCELLED: []   # Final state
    }
    
    return new_status in valid_transitions.get(current_status, [])


async def deduct_stock_from_source(transfer_id: str):
    """Deduct stock from source location when the status is changed to IN_TRANSIT"""
    transfer = await transfer_manager.fetch(transfer_id)
    transfer_items = await transfer_item_manager.fetch_all(
        filters={"transfer_id": transfer_id}
    )
    
    source_outlet_id = transfer.from_outlet_id
    if not source_outlet_id:
        source_outlet_id = await get_default_warehouse_id(engine)

    source_is_factory = False
    if source_outlet_id:
        try:
            src = await outlet_manager.fetch(source_outlet_id)
            source_is_factory = src.outlet_type == OutletType.FACTORY
        except Exception:
            pass

    if not source_is_factory:
        inventory_items_to_update = []
        for item in transfer_items.items:
            source_inventory = await inventory_manager.fetch_all(
                filters={"product_id": item.product_id, "outlet_id": source_outlet_id}
            )
            
            if not source_inventory.items:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"No stock records found at source location for product ID {item.product_id}."
                )
            
            source_item = source_inventory.items[0]
            if source_item.quantity < item.quantity_requested:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Insufficient stock at source for product {item.product_id}. Available: {source_item.quantity}, Requested: {item.quantity_requested}"
                )
            
            inventory_items_to_update.append((source_item, item.quantity_requested))

        # Perform updates
        for source_item, qty in inventory_items_to_update:
            await inventory_manager.update(
                source_item.uid,
                {
                    "quantity": source_item.quantity - qty,
                    "last_updated": datetime.utcnow()
                }
            )


async def add_stock_to_destination(transfer_id: str):
    """Add stock to destination location when the status is changed to DELIVERED"""
    transfer = await transfer_manager.fetch(transfer_id)
    transfer_items = await transfer_item_manager.fetch_all(
        filters={"transfer_id": transfer_id}
    )
    
    for item in transfer_items.items:
        # Add stock at destination location
        dest_inventory = await inventory_manager.fetch_all(
            filters={"product_id": item.product_id, "outlet_id": transfer.to_outlet_id}
        )
        
        if dest_inventory.items:
            dest_item = dest_inventory.items[0]
            await inventory_manager.update(
                dest_item.uid,
                {
                    "quantity": dest_item.quantity + item.quantity_requested,
                    "last_updated": datetime.utcnow()
                }
            )
        else:
            await inventory_manager.create(InventorySchema(
                product_id=item.product_id,
                outlet_id=transfer.to_outlet_id,
                quantity=item.quantity_requested,
                last_updated=datetime.utcnow()
            ))
        
        # Update delivered quantity
        await transfer_item_manager.update(
            item.uid,
            {"quantity_delivered": item.quantity_requested}
        )


async def revert_stock_to_source(transfer_id: str):
    """Revert stock deduction if a transfer is cancelled after being in transit"""
    transfer = await transfer_manager.fetch(transfer_id)
    transfer_items = await transfer_item_manager.fetch_all(
        filters={"transfer_id": transfer_id}
    )
    
    source_outlet_id = transfer.from_outlet_id
    if not source_outlet_id:
        source_outlet_id = await get_default_warehouse_id(engine)

    source_is_factory = False
    if source_outlet_id:
        try:
            src = await outlet_manager.fetch(source_outlet_id)
            source_is_factory = src.outlet_type == OutletType.FACTORY
        except Exception:
            pass

    if not source_is_factory:
        for item in transfer_items.items:
            source_inventory = await inventory_manager.fetch_all(
                filters={"product_id": item.product_id, "outlet_id": source_outlet_id}
            )
            
            if source_inventory.items:
                source_item = source_inventory.items[0]
                await inventory_manager.update(
                    source_item.uid,
                    {
                        "quantity": source_item.quantity + item.quantity_requested,
                        "last_updated": datetime.utcnow()
                    }
                )
            else:
                await inventory_manager.create(InventorySchema(
                    product_id=item.product_id,
                    outlet_id=source_outlet_id,
                    quantity=item.quantity_requested,
                    last_updated=datetime.utcnow()
                ))


async def reserve_transfer_stock(transfer_id: str):
    """Stock reservation logic removed."""
    return


async def release_transfer_stock(transfer_id: str):
    """Stock reservation logic removed."""
    return



