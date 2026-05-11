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
    StockTransferResponse, TransferItemResponse,
    ListResponse, StatusResponse , BulkTransferResponse , StockTransferCreateRequestBulk
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, TransferStatus, HASSAN_OUTLET_ID
from utils.inventory_utils import is_hassan_or_warehouse, sync_unified_inventory
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
            
            # Check stock availability at source location
            source_inventory = await inventory_manager.fetch_all(
                filters={
                    "product_id": item.product_id,
                    "outlet_id": payload.from_outlet_id
                }
            )
            
            if not source_inventory.items:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"No stock available for {product.product_name} at source location"
                )
            
            inventory_item = source_inventory.items[0]
            
            # If transfer is from warehouse/hassan, neglect reserved quantity
            if is_hassan_or_warehouse(payload.from_outlet_id):
                available_stock = inventory_item.quantity
            else:
                available_stock = inventory_item.quantity - inventory_item.reserved_quantity
            
            if available_stock < item.quantity_requested:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Insufficient stock for {product.product_name}. Available: {available_stock}, Requested: {item.quantity_requested}"
                )
            
            validated_items.append({
                "product": product,
                "quantity_requested": item.quantity_requested,
                "inventory_item": inventory_item
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
                
                # Check stock availability at source location
                source_inventory = await inventory_manager.fetch_all(
                    filters={
                        "product_id": product.uid,
                        "outlet_id": from_outlet_id
                    }
                )
                
                if not source_inventory.items:
                    raise ValueError(f"No stock available for {product.product_name} at source location")
                
                inventory_item = source_inventory.items[0]
                
                # If transfer is from warehouse/hassan, neglect reserved quantity
                if is_hassan_or_warehouse(from_outlet_id):
                    available_stock = inventory_item.quantity
                else:
                    available_stock = inventory_item.quantity - inventory_item.reserved_quantity
                
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


@router.get("/pending-approvals", response_model=ListResponse[StockTransferResponse])
async def get_pending_approvals(
    current_user_id: str = Depends(require_roles(
        UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN , UserRole.OUTLET_MANAGER
    ))
):
    """Get all transfers pending approval"""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {"status": TransferStatus.PENDING}
        
        # Warehouse managers only see transfers from warehouse
        if current_user.role == UserRole.WAREHOUSE_MANAGER:
            filters["from_outlet_id"] = None
        
        # Fetch transfers without problematic joins
        transfers = await transfer_manager.fetch_all(filters=filters)
        
        transfer_responses = []
        for transfer in transfers.items:
            try:
                # Get transfer items separately
                items = []
                try:
                    transfer_items = await transfer_item_manager.fetch_all(
                        filters={"transfer_id": transfer.uid}
                    )
                    items = [
                        TransferItemResponse(
                            uid=item.uid,
                            product_id=item.product_id,
                            quantity_requested=item.quantity_requested,
                            quantity_delivered=item.quantity_delivered
                        )
                        for item in transfer_items.items
                    ]
                except Exception as e:
                    print(f"Error fetching items for transfer {transfer.uid}: {str(e)}")
                
                # Build response directly
                transfer_response = StockTransferResponse(
                    uid=transfer.uid,
                    from_outlet_id=transfer.from_outlet_id,
                    to_outlet_id=transfer.to_outlet_id,
                    status=transfer.status,
                    requested_by=transfer.requested_by,
                    approved_by=transfer.approved_by,
                    delivery_person_id=transfer.delivery_person_id,
                    scheduled_date=transfer.scheduled_date,
                    delivered_date=transfer.delivered_date,
                    notes=transfer.notes,
                    items=items,
                    created_at=transfer.created_at
                )
                transfer_responses.append(transfer_response)
                
            except Exception as e:
                print(f"Error processing transfer {transfer.uid}: {str(e)}")
                continue
        
        return ListResponse(items=transfer_responses, count=len(transfer_responses))
    
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
            # Warehouse managers can only handle transfers from warehouse
            if transfer.from_outlet_id is not None:
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
        
        # Handle status-specific logic
        if payload.status == TransferStatus.APPROVED:
            updates["approved_by"] = current_user_id
            # Reserve stock at source location
            await reserve_transfer_stock(transfer_id)
        
        elif payload.status == TransferStatus.DELIVERED:
            from datetime import datetime
            updates["delivered_date"] = datetime.utcnow()
            # Complete the stock transfer - this creates inventory records
            await complete_stock_transfer(transfer_id)
        
        elif payload.status == TransferStatus.CANCELLED:
            if current_user.role not in [UserRole.ADMIN, UserRole.SUPER_ADMIN,UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only admins can cancel transfers"
                )
            # Release reserved stock if any
            await release_transfer_stock(transfer_id)
        
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
                    limit=max(limit + offset, 100)
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
                    limit=max(limit + offset, 100)
                )
                all_transfers_items.extend(outgoing_res.items)
                
            # Deduplicate and sort
            unique_transfers = {t.uid: t for t in all_transfers_items}
            sorted_transfers = sorted(
                unique_transfers.values(), 
                key=lambda x: x.created_at, 
                reverse=True
            )
            
            # Apply date filters
            filtered_transfers = []
            for transfer in sorted_transfers:
                if from_date and transfer.created_at.date() < from_date:
                    continue
                if to_date and transfer.created_at.date() > to_date:
                    continue
                filtered_transfers.append(transfer)
            
            # Apply limit and offset
            final_selection = filtered_transfers[offset : offset + limit]
            
            # Build responses
            transfer_responses = []
            for transfer in final_selection:
                try:
                    transfer_response = await get_transfer_response(transfer.uid)
                    transfer_responses.append(transfer_response)
                except Exception as e:
                    print(f"Error processing transfer {transfer.uid}: {str(e)}")
                    continue
            
            return ListResponse(items=transfer_responses, count=len(filtered_transfers))
        
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
        
        # For non-outlet managers, use normal filtering
        # Fetch transfers without joins to prevent SQLAlchemy loader options error
        transfers = await transfer_manager.fetch_all(
            filters=filters,
            limit=limit,
            offset=offset
        )
        
        transfer_responses = []
        for transfer in transfers.items:
            try:
                # Filter by date range if specified
                if from_date and transfer.created_at.date() < from_date:
                    continue
                if to_date and transfer.created_at.date() > to_date:
                    continue
                
                transfer_response = await get_transfer_response(transfer.uid)
                transfer_responses.append(transfer_response)
            except Exception as e:
                # Log error but continue with other transfers
                print(f"Error processing transfer {transfer.uid}: {str(e)}")
                continue
        
        return ListResponse(items=transfer_responses, count=len(transfer_responses))
    
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
            
        if current_user.role == UserRole.WAREHOUSE_MANAGER and transfer.from_outlet_id is not None:
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
            
        # Refresh and Reserve stock based on updated quantities
        await reserve_transfer_stock(transfer_id)
        
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
    """Helper to build complete transfer response with items"""
    try:
        # Fetch transfer without joins
        transfer = await transfer_manager.fetch(transfer_id)
        
        # Get transfer items
        items = []
        try:
            transfer_items = await transfer_item_manager.fetch_all(
                filters={"transfer_id": transfer_id}
            )
            
            items = [
                TransferItemResponse(
                    uid=item.uid,
                    product_id=item.product_id,
                    quantity_requested=item.quantity_requested,
                    quantity_delivered=item.quantity_delivered
                )
                for item in transfer_items.items
            ]
        except Exception as e:
            print(f"Error fetching transfer items for {transfer_id}: {str(e)}")
            # Continue with empty items list
        
        return StockTransferResponse(
            uid=transfer.uid,
            from_outlet_id=transfer.from_outlet_id,
            to_outlet_id=transfer.to_outlet_id,
            status=transfer.status,
            requested_by=transfer.requested_by,
            approved_by=transfer.approved_by,
            delivery_person_id=transfer.delivery_person_id,
            scheduled_date=transfer.scheduled_date,
            delivered_date=transfer.delivered_date,
            notes=transfer.notes,
            items=items,
            created_at=transfer.created_at
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


async def complete_stock_transfer(transfer_id: str):
    """Complete stock transfer by moving inventory between locations"""
    transfer = await transfer_manager.fetch(transfer_id)
    transfer_items = await transfer_item_manager.fetch_all(
        filters={"transfer_id": transfer_id}
    )
    
    for item in transfer_items.items:
        # Reduce stock at source location
        if transfer.from_outlet_id is None or is_hassan_or_warehouse(transfer.from_outlet_id):
            await sync_unified_inventory(
                inventory_manager,
                InventorySchema,
                product_id=item.product_id,
                target_outlet_id=transfer.from_outlet_id,
                quantity_delta=-item.quantity_requested,
                reserved_delta=-item.quantity_requested
            )
        elif transfer.from_outlet_id:  # Only if transferring from another outlet
            source_inventory = await inventory_manager.fetch_all(
                filters={
                    "product_id": item.product_id,
                    "outlet_id": transfer.from_outlet_id
                }
            )
            
            if source_inventory.items:
                source_item = source_inventory.items[0]
                new_quantity = source_item.quantity - item.quantity_requested
                new_reserved = source_item.reserved_quantity - item.quantity_requested
                
                await inventory_manager.update(
                    source_item.uid,
                    {
                        "quantity": max(0, new_quantity),
                        "reserved_quantity": max(0, new_reserved),
                        "last_updated": datetime.utcnow()
                    }
                )
        
        # Add stock at destination location
        if is_hassan_or_warehouse(transfer.to_outlet_id):
            await sync_unified_inventory(
                inventory_manager,
                InventorySchema,
                product_id=item.product_id,
                target_outlet_id=transfer.to_outlet_id,
                quantity_delta=item.quantity_requested
            )
        else:
            dest_inventory = await inventory_manager.fetch_all(
                filters={
                    "product_id": item.product_id,
                    "outlet_id": transfer.to_outlet_id
                }
            )
            
            if dest_inventory.items:
                # Update existing inventory
                dest_item = dest_inventory.items[0]
                new_quantity = dest_item.quantity + item.quantity_requested
                
                await inventory_manager.update(
                    dest_item.uid,
                    {
                        "quantity": new_quantity,
                        "last_updated": datetime.utcnow()
                    }
                )
            else:
                # Create new inventory record at destination
                new_inventory = InventorySchema(
                    product_id=item.product_id,
                    outlet_id=transfer.to_outlet_id,
                    quantity=item.quantity_requested,
                    reserved_quantity=0,
                    last_updated=datetime.utcnow()
                )
                await inventory_manager.create(new_inventory)
        
        # Update delivered quantity
        await transfer_item_manager.update(
            item.uid,
            {"quantity_delivered": item.quantity_requested}
        )


async def reserve_transfer_stock(transfer_id: str):
    """Reserve stock at source location when transfer is approved"""
    transfer = await transfer_manager.fetch(transfer_id)
    transfer_items = await transfer_item_manager.fetch_all(
        filters={"transfer_id": transfer_id}
    )
    
    for item in transfer_items.items:
        # Find inventory at source location
        if is_hassan_or_warehouse(transfer.from_outlet_id):
            await sync_unified_inventory(
                inventory_manager,
                InventorySchema,
                product_id=item.product_id,
                target_outlet_id=transfer.from_outlet_id,
                reserved_delta=item.quantity_requested
            )
        else:
            inventory_items = await inventory_manager.fetch_all(
                filters={
                    "product_id": item.product_id,
                    "outlet_id": transfer.from_outlet_id
                }
            )
            
            if inventory_items.items:
                inventory_item = inventory_items.items[0]
                new_reserved = inventory_item.reserved_quantity + item.quantity_requested
                
                await inventory_manager.update(
                    inventory_item.uid,
                    {
                        "reserved_quantity": new_reserved,
                        "last_updated": datetime.utcnow()
                    }
                )


async def release_transfer_stock(transfer_id: str):
    """Release reserved stock when transfer is cancelled"""
    transfer = await transfer_manager.fetch(transfer_id)
    
    # Only release if transfer was approved (stock was reserved)
    if transfer.status not in [TransferStatus.APPROVED, TransferStatus.IN_TRANSIT]:
        return
    
    transfer_items = await transfer_item_manager.fetch_all(
        filters={"transfer_id": transfer_id}
    )
    
    for item in transfer_items.items:
        # Find inventory at source location
        if is_hassan_or_warehouse(transfer.from_outlet_id):
            await sync_unified_inventory(
                inventory_manager,
                InventorySchema,
                product_id=item.product_id,
                target_outlet_id=transfer.from_outlet_id,
                reserved_delta=-item.quantity_requested
            )
        elif transfer.from_outlet_id:  # Only if transferring from another outlet
            inventory_items = await inventory_manager.fetch_all(
                filters={
                    "product_id": item.product_id,
                    "outlet_id": transfer.from_outlet_id
                }
            )
            
            if inventory_items.items:
                inventory_item = inventory_items.items[0]
                new_reserved = inventory_item.reserved_quantity - item.quantity_requested
                
                await inventory_manager.update(
                    inventory_item.uid,
                    {
                        "reserved_quantity": max(0, new_reserved),
                        "last_updated": datetime.utcnow()
                    }
                )



