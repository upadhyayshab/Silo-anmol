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
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, TransferStatus, OutletType
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


@router.post("", response_model=StockTransferResponse)
async def create_transfer_request(
    payload: StockTransferCreateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Create stock transfer request
    - Outlet managers can request from warehouse to their outlet
    - Warehouse managers can initiate transfers to any outlet
    - Admins can create any transfer
    """
    try:
        # Get current user to determine permissions
        current_user = await user_manager.fetch(current_user_id)

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
        
        # Role-based validation
        if current_user.role == UserRole.OUTLET_MANAGER:
            # Outlet managers must be involved in the transfer (either as source or destination)
            if current_user.outlet_id != payload.to_outlet_id and current_user.outlet_id != payload.from_outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only create transfers where your outlet is either the source or destination"
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
    current_user_id: str = Depends(require_roles(
        UserRole.ADMIN, UserRole.SUPER_ADMIN,UserRole.WAREHOUSE_MANAGER
    ))
):
    """
    Mass upload stock transfer requests.
    - Restricted to Admins, Super Admins, Warehouse Managers.
    - Processes the batch and returns a summary of successes and failures.
    """
    # Fetch current user once for the whole batch
    current_user = await user_manager.fetch(current_user_id)
    
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
    current_user_id: str = Depends(require_roles(
        UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN , UserRole.OUTLET_MANAGER
    ))
):
    """Get all transfers pending approval"""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {"status": TransferStatus.PENDING}
        
        # Warehouse managers see all transfers (since warehouse is now Hassan outlet)
        
        # Fetch transfers without problematic joins
        transfers = await transfer_manager.fetch_all(filters=filters, limit=limit, offset=offset)
        
        # Build responses in batch to avoid slow N+1 queries
        transfer_responses = await get_transfer_responses_batch(transfers.items)
        
        return ListResponse(items=transfer_responses, count=transfers.count)
    
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
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Get specific transfer details"""
    try:
        transfer = await transfer_manager.fetch(transfer_id)
        
        # Check access permissions
        current_user = await user_manager.fetch(current_user_id)
        
        # Role-based access control
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id and (
                transfer.from_outlet_id != current_user.outlet_id and 
                transfer.to_outlet_id != current_user.outlet_id
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view transfers involving your outlet"
                )
        
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
    current_user_id: str = Depends(require_roles(
        UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Get transfer summary report"""
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
    current_user_id: str = Depends(get_current_user_id),
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER))
):
    """Approve stock transfer request"""
    try:
        # Get transfer
        transfer = await transfer_manager.fetch(transfer_id)
        
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
    current_user_id: str = Depends(get_current_user_id),
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER))
):
    """Update transfer status"""
    try:
        # Get current user
        current_user = await user_manager.fetch(current_user_id)
        
        # Get current transfer to validate transition
        transfer = await transfer_manager.fetch(transfer_id)
        
        # Validate status transition
        if not is_valid_transfer_status_transition(transfer.status, payload.status):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status transition from {transfer.status} to {payload.status}"
            )
        
        # Role-based status update validation
        if current_user.role == UserRole.WAREHOUSE_MANAGER:
            # Warehouse managers can only handle transfers from their assigned warehouse
            if transfer.from_outlet_id is not None and current_user.outlet_id is not None and transfer.from_outlet_id != current_user.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only manage transfers from warehouse"
                )
            
            # Can approve, ship, but not deliver
            if payload.status == TransferStatus.DELIVERED:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only delivery personnel or admins can mark transfers as delivered"
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
            if current_user.role not in [UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only authorized roles can cancel transfers"
                )
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
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Get transfer requests with filters
    Role-based filtering applied automatically
    """
    try:
        # Get current user to determine access level
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {}
        
        # Role-based filtering
        if current_user.role == UserRole.OUTLET_MANAGER:
            # Outlet managers see transfers involving their outlet
            if not current_user.outlet_id:
                return ListResponse(items=[], count=0)
            
            my_id = current_user.outlet_id
            
            # Base filters common to both queries
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
                
            all_transfers_items = []
            
            # Scenario 1: Incoming 
            if to_outlet_id is None or to_outlet_id == my_id:
                in_filters = base_filters.copy()
                in_filters["to_outlet_id"] = my_id
                if from_outlet_id is not None:
                    in_filters["from_outlet_id"] = from_outlet_id
                
                # Fetch more than limit to allow for combined filtering/sorting
                incoming_res = await transfer_manager.fetch_all(
                    filters=in_filters, 
                    limit=max(limit + offset, 100),
                    sorts=["-created_at"]
                )
                all_transfers_items.extend(incoming_res.items)
            
            # Scenario 2: Outgoing 
            if from_outlet_id is None or from_outlet_id == my_id:
                out_filters = base_filters.copy()
                out_filters["from_outlet_id"] = my_id
                if to_outlet_id is not None:
                    out_filters["to_outlet_id"] = to_outlet_id
                
                outgoing_res = await transfer_manager.fetch_all(
                    filters=out_filters,
                    limit=max(limit + offset, 100),
                    sorts=["-created_at"]
                )
                all_transfers_items.extend(outgoing_res.items)
                
            # Deduplicate and sort
            unique_transfers = {t.uid: t for t in all_transfers_items}
            sorted_transfers = sorted(
                unique_transfers.values(), 
                key=lambda x: x.created_at, 
                reverse=True
            )
            # Apply limit and offset
            final_selection = sorted_transfers[offset : offset + limit]
            
            # Build responses in batch to avoid slow N+1 queries
            transfer_responses = await get_transfer_responses_batch(final_selection)
            
            return ListResponse(items=transfer_responses, count=len(sorted_transfers))
        
        # Apply filters for admins or if user has broader access
        if current_user.role in [UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.WAREHOUSE_MANAGER]:
            if transfer_status:
                filters["status"] = transfer_status
            if from_outlet_id is not None:
                filters["from_outlet_id"] = from_outlet_id
            if to_outlet_id:
                filters["to_outlet_id"] = to_outlet_id
            if requested_by:
                filters["requested_by"] = requested_by
        elif transfer_status:
            filters["status"] = transfer_status
            
        if from_date or to_date:
            from datetime import datetime, time
            if "created_at" not in filters:
                filters["created_at"] = {}
            if from_date:
                filters["created_at"][">="] = datetime.combine(from_date, time.min)
            if to_date:
                filters["created_at"]["<="] = datetime.combine(to_date, time.max)
        
        # For non-outlet managers, use normal filtering
        # Fetch transfers without joins to prevent SQLAlchemy loader options error
        transfers = await transfer_manager.fetch_all(
            filters=filters,
            limit=limit,
            offset=offset,
            sorts=["-created_at"]
        )
        
        # Build responses in batch to avoid slow N+1 queries
        transfer_responses = await get_transfer_responses_batch(transfers.items)
        
        return ListResponse(items=transfer_responses, count=transfers.count)
    
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
    current_user_id: str = Depends(require_roles(
        UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN,UserRole.OUTLET_MANAGER ,UserRole.SUPER_ADMIN
    ))
):
    """
    Approve transfer request and potentially modify requested quantities.
    Available to warehouse managers and admins.
    """
    try:
        current_user = await user_manager.fetch(current_user_id)
        transfer = await transfer_manager.fetch(transfer_id)
        
        # Validations
        if transfer.status != TransferStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Only PENDING transfers can be approved. Current status: {transfer.status}"
            )
            
        if current_user.role == UserRole.WAREHOUSE_MANAGER and transfer.from_outlet_id is not None and transfer.from_outlet_id != current_user.outlet_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Warehouse managers can only approve transfers from warehouse"
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



