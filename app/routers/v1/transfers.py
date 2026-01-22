from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional
from datetime import datetime, date

from config import get_settings, get_engine
from managers import (
    StockTransferOrderManager, TransferItemManager, InventoryManager,
    ProductManager, OutletManager, UserManager,
    StockTransferOrderSchema, TransferItemSchema
)
from models import (
    StockTransferCreateRequest, StockTransferStatusUpdateRequest,
    StockTransferResponse, TransferItemResponse,
    ListResponse, StatusResponse
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, TransferStatus
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
            # Outlet managers can only request transfers to their own outlet from warehouse
            if current_user.outlet_id != payload.to_outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only request transfers to your own outlet"
                )
            if payload.from_outlet_id is not None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Outlet managers can only request transfers from warehouse"
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


@router.get("/test-deployment")
async def test_deployment():
    """Test endpoint to verify deployment"""
    return {"message": "Code updated successfully", "timestamp": "2026-01-22-06:55"}


@router.get("/pending-approvals", response_model=ListResponse[StockTransferResponse])
async def get_pending_approvals(
    current_user_id: str = Depends(require_roles(
        UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
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


@router.put("/{transfer_id}/status", response_model=StockTransferResponse)
async def update_transfer_status(
    transfer_id: str,
    payload: StockTransferStatusUpdateRequest,
    current_user_id: str = Depends(get_current_user_id),
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER))
):
    """Update transfer status"""
    try:
        updates = {
            "status": payload.status,
            "notes": payload.notes
        }
        
        # Set delivery date if delivered
        if payload.status == TransferStatus.DELIVERED:
            from datetime import datetime
            updates["delivered_date"] = datetime.utcnow()
        
        updated_transfer = await transfer_manager.update(transfer_id, updates)
        return updated_transfer
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to update transfer status: {str(e)}"
        )


# GENERIC ROUTE LAST (after all specific routes)

@router.get("", response_model=ListResponse[StockTransferResponse])
async def get_transfers(
    status: Optional[TransferStatus] = None,
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
            if current_user.outlet_id:
                # Show transfers TO their outlet (requests they made) or FROM their outlet
                filters["$or"] = [
                    {"to_outlet_id": current_user.outlet_id},
                    {"from_outlet_id": current_user.outlet_id}
                ]
        elif current_user.role == UserRole.WAREHOUSE_MANAGER:
            # Warehouse managers see transfers involving warehouse (from_outlet_id = NULL)
            filters["from_outlet_id"] = None
        
        # Apply additional filters for admins or if user has broader access
        if current_user.role in [UserRole.ADMIN, UserRole.SUPER_ADMIN]:
            if status:
                filters["status"] = status
            if from_outlet_id is not None:
                filters["from_outlet_id"] = from_outlet_id
            if to_outlet_id:
                filters["to_outlet_id"] = to_outlet_id
            if requested_by:
                filters["requested_by"] = requested_by
        elif status:
            filters["status"] = status
        
        # Fetch transfers with eager loading to prevent session issues
        transfers = await transfer_manager.fetch_all(
            filters=filters,
            limit=limit,
            offset=offset,
            joins=["items", "to_outlet", "from_outlet", "requester"]
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


# Duplicate /{transfer_id} route removed - moved to top of file
    """Get specific transfer details"""
    try:
        transfer = await transfer_manager.fetch(transfer_id)
        
        # Check access permissions
        current_user = await user_manager.fetch(current_user_id)
        
        if current_user.role == UserRole.OUTLET_MANAGER:
            # Can only view transfers involving their outlet
            if (current_user.outlet_id != transfer.to_outlet_id and 
                current_user.outlet_id != transfer.from_outlet_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view transfers involving your outlet"
                )
        elif current_user.role == UserRole.WAREHOUSE_MANAGER:
            # Can only view transfers involving warehouse
            if transfer.from_outlet_id is not None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view transfers from warehouse"
                )
        
        return await get_transfer_response(transfer_id)
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transfer not found"
        )


async def get_transfer_response(transfer_id: str) -> StockTransferResponse:
    """Helper to build complete transfer response with items"""
    try:
        # Fetch transfer without joins (fetch relationships separately)
        transfer = await transfer_manager.fetch(transfer_id)
        
        # Get transfer items with error handling
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
        # If transfer fetch fails, handle the error
        try:
            transfer = await transfer_manager.fetch(transfer_id)
            
            # Get items separately
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
            except:
                pass
            
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
        except Exception as inner_e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Transfer not found: {transfer_id}"
            )


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
        
        # Handle status-specific logic
        update_data = {
            "status": payload.status,
            "notes": payload.notes
        }
        
        if payload.status == TransferStatus.APPROVED:
            update_data["approved_by"] = current_user_id
            # Reserve stock at source location
            await reserve_transfer_stock(transfer_id)
        
        elif payload.status == TransferStatus.IN_TRANSIT:
            if not transfer.approved_by:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Transfer must be approved before shipping"
                )
        
        elif payload.status == TransferStatus.DELIVERED:
            update_data["delivered_date"] = datetime.utcnow()
            # Complete the stock transfer
            await complete_stock_transfer(transfer_id)
        
        elif payload.status == TransferStatus.CANCELLED:
            if current_user.role != UserRole.ADMIN and current_user.role != UserRole.SUPER_ADMIN:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only admins can cancel transfers"
                )
            # Release reserved stock if any
            await release_transfer_stock(transfer_id)
        
        await transfer_manager.update(transfer_id, update_data)
        
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


async def reserve_transfer_stock(transfer_id: str):
    """Reserve stock at source location when transfer is approved"""
    transfer = await transfer_manager.fetch(transfer_id)
    transfer_items = await transfer_item_manager.fetch_all(
        filters={"transfer_id": transfer_id}
    )
    
    for item in transfer_items.items:
        # Find inventory at source location
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


async def complete_stock_transfer(transfer_id: str):
    """Complete stock transfer by moving inventory between locations"""
    transfer = await transfer_manager.fetch(transfer_id)
    transfer_items = await transfer_item_manager.fetch_all(
        filters={"transfer_id": transfer_id}
    )
    
    for item in transfer_items.items:
        # Reduce stock at source location
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
            from managers import InventorySchema
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


@router.put("/{transfer_id}/assign-delivery", response_model=StatusResponse)
async def assign_delivery_person(
    transfer_id: str,
    delivery_person_id: str,
    _: str = Depends(require_roles(UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN))
):
    """Assign delivery person to approved transfer"""
    try:
        transfer = await transfer_manager.fetch(transfer_id)
        
        if transfer.status != TransferStatus.APPROVED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Can only assign delivery person to approved transfers"
            )
        
        # Verify delivery person exists and has appropriate role
        try:
            delivery_person = await user_manager.fetch(delivery_person_id)
            if not delivery_person.is_active:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Delivery person is not active"
                )
        except:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Delivery person not found"
            )
        
        await transfer_manager.update(
            transfer_id,
            {"delivery_person_id": delivery_person_id}
        )
        
        return StatusResponse(
            status="ok",
            message=f"Delivery person assigned to transfer"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to assign delivery person: {str(e)}"
        )


# Duplicate /reports/transfer-summary route removed - moved to top of file
    """
    Get transfer summary report
    """
    try:
        # Set default date range if not provided
        if not from_date:
            from_date = date.today().replace(day=1)  # First day of current month
        if not to_date:
            to_date = date.today()
        
        filters = {}
        if outlet_id:
            filters["$or"] = [
                {"from_outlet_id": outlet_id},
                {"to_outlet_id": outlet_id}
            ]
        
        transfers = await transfer_manager.fetch_all(filters=filters)
        
        # Calculate summary statistics
        status_counts = {status.value: 0 for status in TransferStatus}
        total_transfers = 0
        completed_transfers = 0
        pending_approvals = 0
        
        for transfer in transfers.items:
            if transfer.created_at.date() >= from_date and transfer.created_at.date() <= to_date:
                total_transfers += 1
                status_counts[transfer.status.value] += 1
                
                if transfer.status == TransferStatus.DELIVERED:
                    completed_transfers += 1
                elif transfer.status == TransferStatus.PENDING:
                    pending_approvals += 1
        
        return {
            "period": {
                "from_date": from_date,
                "to_date": to_date
            },
            "summary": {
                "total_transfers": total_transfers,
                "completed_transfers": completed_transfers,
                "pending_approvals": pending_approvals,
                "completion_rate": round((completed_transfers / total_transfers * 100) if total_transfers > 0 else 0, 2)
            },
            "status_breakdown": status_counts
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate transfer summary: {str(e)}"
        )


