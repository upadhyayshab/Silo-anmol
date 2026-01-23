from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional
from datetime import datetime

from config import get_settings, get_engine
from managers import InventoryManager, ProductManager, OutletManager, UserManager, InventorySchema
from models import (
    InventoryResponse, StockAdjustmentRequest,
    ListResponse, StatusResponse
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole

settings = get_settings()
engine = get_engine(settings.name)
inventory_manager = InventoryManager(engine)
product_manager = ProductManager(engine)
outlet_manager = OutletManager(engine)
user_manager = UserManager(engine)

router = APIRouter(prefix="/inventory", tags=["Inventory Management"])


# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

@router.get("/product/{product_id}", response_model=ListResponse[InventoryResponse])
async def get_product_inventory(
    product_id: str,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Get inventory for a specific product across all outlets"""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {"product_id": product_id}
        
        # Role-based filtering
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id:
                filters["outlet_id"] = current_user.outlet_id
        
        inventory_items = await inventory_manager.fetch_all(filters=filters)
        
        inventory_responses = []
        for item in inventory_items.items:
            try:
                product = await product_manager.fetch(item.product_id)
                outlet = None
                if item.outlet_id:
                    outlet = await outlet_manager.fetch(item.outlet_id)
                
                inventory_responses.append(InventoryResponse(
                    uid=item.uid,
                    product_id=item.product_id,
                    product_name=product.product_name,
                    outlet_id=item.outlet_id,
                    outlet_name=outlet.outlet_name if outlet else "Warehouse",
                    quantity=item.quantity,
                    reserved_quantity=item.reserved_quantity,
                    available_quantity=item.quantity - item.reserved_quantity,
                    min_stock_level=product.min_stock_level,
                    last_updated=item.last_updated
                ))
            except Exception as e:
                print(f"Error processing inventory item {item.uid}: {str(e)}")
                continue
        
        return ListResponse(items=inventory_responses, count=len(inventory_responses))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch product inventory: {str(e)}"
        )


@router.get("/low-stock", response_model=ListResponse[InventoryResponse])
async def get_low_stock_alerts(
    outlet_id: Optional[str] = None,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Get items with low stock levels"""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {}
        
        # Role-based filtering
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id:
                filters["outlet_id"] = current_user.outlet_id
        elif outlet_id:
            filters["outlet_id"] = outlet_id
        
        inventory_items = await inventory_manager.fetch_all(filters=filters)
        
        low_stock_items = []
        for item in inventory_items.items:
            try:
                product = await product_manager.fetch(item.product_id)
                
                # Check if stock is below minimum level
                available_quantity = item.quantity - item.reserved_quantity
                if available_quantity <= product.min_stock_level:
                    outlet = None
                    if item.outlet_id:
                        outlet = await outlet_manager.fetch(item.outlet_id)
                    
                    low_stock_items.append(InventoryResponse(
                        uid=item.uid,
                        product_id=item.product_id,
                        product_name=product.product_name,
                        outlet_id=item.outlet_id,
                        outlet_name=outlet.outlet_name if outlet else "Warehouse",
                        quantity=item.quantity,
                        reserved_quantity=item.reserved_quantity,
                        available_quantity=available_quantity,
                        min_stock_level=product.min_stock_level,
                        last_updated=item.last_updated
                    ))
            except Exception as e:
                print(f"Error processing inventory item {item.uid}: {str(e)}")
                continue
        
        return ListResponse(items=low_stock_items, count=len(low_stock_items))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch low stock items: {str(e)}"
        )


@router.get("/reserved", response_model=ListResponse[InventoryResponse])
async def get_reserved_stock(
    outlet_id: Optional[str] = None,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Get items with reserved stock"""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {}
        
        # Role-based filtering
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id:
                filters["outlet_id"] = current_user.outlet_id
        elif outlet_id:
            filters["outlet_id"] = outlet_id
        
        inventory_items = await inventory_manager.fetch_all(filters=filters)
        
        reserved_items = []
        for item in inventory_items.items:
            if item.reserved_quantity > 0:
                try:
                    product = await product_manager.fetch(item.product_id)
                    outlet = None
                    if item.outlet_id:
                        outlet = await outlet_manager.fetch(item.outlet_id)
                    
                    reserved_items.append(InventoryResponse(
                        uid=item.uid,
                        product_id=item.product_id,
                        product_name=product.product_name,
                        outlet_id=item.outlet_id,
                        outlet_name=outlet.outlet_name if outlet else "Warehouse",
                        quantity=item.quantity,
                        reserved_quantity=item.reserved_quantity,
                        available_quantity=item.quantity - item.reserved_quantity,
                        min_stock_level=product.min_stock_level,
                        last_updated=item.last_updated
                    ))
                except Exception as e:
                    print(f"Error processing inventory item {item.uid}: {str(e)}")
                    continue
        
        return ListResponse(items=reserved_items, count=len(reserved_items))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch reserved inventory: {str(e)}"
        )


# GENERIC ROUTE LAST (after all specific routes)

@router.get("", response_model=ListResponse[InventoryResponse])
async def get_inventory(
    outlet_id: Optional[str] = None,  # NULL for warehouse
    product_id: Optional[str] = None,
    low_stock_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    _: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER,
        UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT
    ))
):
    """
    Get inventory across locations with filters
    - outlet_id: specific outlet (NULL for warehouse)
    - product_id: specific product
    - low_stock_only: only show items below minimum stock level
    """
    try:
        filters = {}
        if outlet_id is not None:
            filters["outlet_id"] = outlet_id
        if product_id:
            filters["product_id"] = product_id
        
        inventory_items = await inventory_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=filters if filters else None
        )
        
        inventory_responses = []
        
        for item in inventory_items.items:
            available_quantity = item.quantity - item.reserved_quantity
            
            # Filter low stock items if requested
            if low_stock_only:
                # Get product min_stock_level
                product = await product_manager.fetch(item.product_id)
                if available_quantity >= product.min_stock_level:
                    continue
            
            inventory_responses.append(
                InventoryResponse(
                    uid=item.uid,
                    product_id=item.product_id,
                    outlet_id=item.outlet_id,
                    quantity=item.quantity,
                    reserved_quantity=item.reserved_quantity,
                    available_quantity=available_quantity,
                    last_updated=item.last_updated
                )
            )
        
        return ListResponse(items=inventory_responses, count=len(inventory_responses))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch inventory: {str(e)}"
        )


# Duplicate routes removed - moved to top of file for proper ordering


# Duplicate low-stock route removed


# Duplicate reserved route removed


@router.post("/adjust", response_model=StatusResponse)
async def adjust_stock(
    payload: StockAdjustmentRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER,
        UserRole.OUTLET_MANAGER
    ))
):
    """
    Manual stock adjustment with reason tracking
    Positive quantity_change = stock addition
    Negative quantity_change = stock reduction
    """
    try:
        # Verify product exists
        try:
            await product_manager.fetch(payload.product_id)
        except:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Product not found"
            )
        
        # Verify outlet exists (if specified)
        if payload.outlet_id:
            try:
                await outlet_manager.fetch(payload.outlet_id)
            except:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Outlet not found"
                )
        
        # Find existing inventory record
        existing_inventory = await inventory_manager.fetch_all(
            filters={
                "product_id": payload.product_id,
                "outlet_id": payload.outlet_id
            }
        )
        
        if existing_inventory.items:
            # Update existing inventory
            inventory_item = existing_inventory.items[0]
            new_quantity = inventory_item.quantity + payload.quantity_change
            
            # Prevent negative stock
            if new_quantity < 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Insufficient stock. Current: {inventory_item.quantity}, Requested change: {payload.quantity_change}"
                )
            
            # Prevent reducing below reserved quantity
            if new_quantity < inventory_item.reserved_quantity:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot reduce stock below reserved quantity. Reserved: {inventory_item.reserved_quantity}"
                )
            
            await inventory_manager.update(
                inventory_item.uid,
                {
                    "quantity": new_quantity,
                    "last_updated": datetime.utcnow()
                }
            )
            
            return StatusResponse(
                status="ok",
                message=f"Stock adjusted successfully. New quantity: {new_quantity}"
            )
        
        else:
            # Create new inventory record (only for positive adjustments)
            if payload.quantity_change <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot create inventory with zero or negative quantity"
                )
            
            new_inventory = InventorySchema(
                product_id=payload.product_id,
                outlet_id=payload.outlet_id,
                quantity=payload.quantity_change,
                reserved_quantity=0,
                last_updated=datetime.utcnow()
            )
            
            await inventory_manager.create(new_inventory)
            
            return StatusResponse(
                status="ok",
                message=f"New inventory created with quantity: {payload.quantity_change}"
            )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stock adjustment failed: {str(e)}"
        )


@router.post("/reserve", response_model=StatusResponse)
async def reserve_stock(
    product_id: str,
    outlet_id: Optional[str],
    quantity: int,
    _: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER,
        UserRole.OUTLET_MANAGER, UserRole.TELECALLER
    ))
):
    """
    Reserve stock for pending orders
    Internal endpoint used by order management
    """
    try:
        if quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Quantity must be positive"
            )
        
        # Find inventory record
        inventory_items = await inventory_manager.fetch_all(
            filters={
                "product_id": product_id,
                "outlet_id": outlet_id
            }
        )
        
        if not inventory_items.items:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inventory not found for this product and location"
            )
        
        inventory_item = inventory_items.items[0]
        available_quantity = inventory_item.quantity - inventory_item.reserved_quantity
        
        if available_quantity < quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient stock. Available: {available_quantity}, Requested: {quantity}"
            )
        
        # Update reserved quantity
        new_reserved = inventory_item.reserved_quantity + quantity
        await inventory_manager.update(
            inventory_item.uid,
            {
                "reserved_quantity": new_reserved,
                "last_updated": datetime.utcnow()
            }
        )
        
        return StatusResponse(
            status="ok",
            message=f"Reserved {quantity} units. Total reserved: {new_reserved}"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stock reservation failed: {str(e)}"
        )


@router.post("/release", response_model=StatusResponse)
async def release_reserved_stock(
    product_id: str,
    outlet_id: Optional[str],
    quantity: int,
    _: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER,
        UserRole.OUTLET_MANAGER
    ))
):
    """
    Release reserved stock (e.g., when order is cancelled)
    Internal endpoint used by order management
    """
    try:
        if quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Quantity must be positive"
            )
        
        # Find inventory record
        inventory_items = await inventory_manager.fetch_all(
            filters={
                "product_id": product_id,
                "outlet_id": outlet_id
            }
        )
        
        if not inventory_items.items:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inventory not found"
            )
        
        inventory_item = inventory_items.items[0]
        
        if inventory_item.reserved_quantity < quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot release more than reserved. Reserved: {inventory_item.reserved_quantity}, Requested: {quantity}"
            )
        
        # Update reserved quantity
        new_reserved = inventory_item.reserved_quantity - quantity
        await inventory_manager.update(
            inventory_item.uid,
            {
                "reserved_quantity": new_reserved,
                "last_updated": datetime.utcnow()
            }
        )
        
        return StatusResponse(
            status="ok",
            message=f"Released {quantity} units. Total reserved: {new_reserved}"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stock release failed: {str(e)}"
        )


@router.post("/consume", response_model=StatusResponse)
async def consume_reserved_stock(
    product_id: str,
    outlet_id: Optional[str],
    quantity: int,
    _: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER,
        UserRole.OUTLET_MANAGER
    ))
):
    """
    Consume reserved stock (e.g., when order is delivered or invoice is created)
    Reduces both quantity and reserved_quantity
    Internal endpoint used by order/invoice management
    """
    try:
        if quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Quantity must be positive"
            )
        
        # Find inventory record
        inventory_items = await inventory_manager.fetch_all(
            filters={
                "product_id": product_id,
                "outlet_id": outlet_id
            }
        )
        
        if not inventory_items.items:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inventory not found"
            )
        
        inventory_item = inventory_items.items[0]
        
        if inventory_item.reserved_quantity < quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot consume more than reserved. Reserved: {inventory_item.reserved_quantity}, Requested: {quantity}"
            )
        
        if inventory_item.quantity < quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient total stock. Available: {inventory_item.quantity}, Requested: {quantity}"
            )
        
        # Update both quantity and reserved quantity
        new_quantity = inventory_item.quantity - quantity
        new_reserved = inventory_item.reserved_quantity - quantity
        
        await inventory_manager.update(
            inventory_item.uid,
            {
                "quantity": new_quantity,
                "reserved_quantity": new_reserved,
                "last_updated": datetime.utcnow()
            }
        )
        
        return StatusResponse(
            status="ok",
            message=f"Consumed {quantity} units. New quantity: {new_quantity}, Reserved: {new_reserved}"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stock consumption failed: {str(e)}"
        )
