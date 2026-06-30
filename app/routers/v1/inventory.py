from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional
from datetime import datetime

from config import get_settings, get_engine
from managers import (
    InventoryManager, ProductManager, OutletManager, UserManager, 
    InventorySchema, TransferItemSchema, StockTransferOrderSchema,
    CustomerOrderSchema, OrderItemSchema
)
from models import (
    InventoryResponse, InventoryAuditResponse, StockAdjustmentRequest,
    ListResponse, StatusResponse
)
from utils.auth import require_permission, apply_scope, AuthContext
from utils.permissions import Permission
from utils.constants import UserRole, TransferStatus, OrderStatus, OutletType
from utils.warehouse_utils import get_default_warehouse_id

import sqlalchemy as db
from sqlalchemy import func, and_
from sqlalchemy.ext.asyncio import AsyncSession

settings = get_settings()
engine = get_engine(settings.name)
inventory_manager = InventoryManager(engine)
product_manager = ProductManager(engine)
outlet_manager = OutletManager(engine)
user_manager = UserManager(engine)



# Helpers now imported from utils.inventory_utils


router = APIRouter(prefix="/inventory", tags=["Inventory Management"])


# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

@router.get("/product/{product_id}", response_model=ListResponse[InventoryResponse])
async def get_product_inventory(
    product_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.INVENTORY_READ)),
):
    """Get inventory for a specific product across all outlets"""
    try:
        # Row scope: outlet mgr -> own outlet; cluster/state -> their outlets; global
        # (admin/warehouse/accountant) -> all. Replaces the old role check + manual filter.
        filters = await apply_scope({"product_id": product_id}, ctx)

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
                    outlet_id=item.outlet_id,
                    quantity=item.quantity,
                    reserved_quantity=0,
                    available_quantity=item.quantity,
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
    ctx: AuthContext = Depends(require_permission(Permission.INVENTORY_READ)),
):
    """Get items with low stock levels"""
    try:
        # Honor an explicit outlet_id for global callers; apply_scope then overrides it
        # for scoped roles (so they can't query outside their scope).
        filters = {"outlet_id": outlet_id} if outlet_id else {}
        filters = await apply_scope(filters, ctx)

        inventory_items = await inventory_manager.fetch_all(filters=filters)
        
        low_stock_items = []
        for item in inventory_items.items:
            try:
                product = await product_manager.fetch(item.product_id)
                
                # Check if stock is below minimum level
                available_quantity = item.quantity
                if available_quantity <= product.min_stock_level:
                    outlet = None
                    if item.outlet_id:
                        outlet = await outlet_manager.fetch(item.outlet_id)
                    
                    low_stock_items.append(InventoryResponse(
                        uid=item.uid,
                        product_id=item.product_id,
                        outlet_id=item.outlet_id,
                        quantity=item.quantity,
                        reserved_quantity=0,
                        available_quantity=item.quantity,
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
    ctx: AuthContext = Depends(require_permission(Permission.INVENTORY_READ)),
):
    """Get items with reserved stock"""
    try:
        # Reserved quantity logic removed. Always returning empty list.
        return ListResponse(items=[], count=0)
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch reserved inventory: {str(e)}"
        )


# GENERIC ROUTE LAST (after all specific routes)

@router.get("", response_model=ListResponse[InventoryAuditResponse])
async def get_inventory(
    outlet_id: Optional[str] = None,  # NULL for warehouse
    product_id: Optional[str] = None,
    low_stock_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    ctx: AuthContext = Depends(require_permission(Permission.INVENTORY_READ)),
):
    """
    Get inventory across locations with filters
    - outlet_id: specific outlet
    - product_id: specific product
    - low_stock_only: only show items below minimum stock level
    """
    try:
        # Resolve the warehouse ID at runtime (supports multiple warehouses)
        warehouse_id = await get_default_warehouse_id(engine)

        # Row scope: None=global (admin/warehouse/accountant), a scalar (outlet mgr) or a
        # list (cluster/state) of outlet ids. ANDed onto the query so a scoped caller
        # can't see — or query for — out-of-scope outlets.
        scope_outlet = (await apply_scope({}, ctx)).get("outlet_id")

        # Define coalesced expressions for consistent grouping
        # NULL outlet_id rows are legacy records that belong to the warehouse
        to_outlet_expr = func.coalesce(StockTransferOrderSchema.to_outlet_id, warehouse_id)
        from_outlet_expr = func.coalesce(StockTransferOrderSchema.from_outlet_id, warehouse_id)
        assigned_outlet_expr = func.coalesce(CustomerOrderSchema.assigned_outlet_id, warehouse_id)

        # 1. CTE: 'total_received' per product & to_outlet
        transfer_in_cte = (
            db.select(
                TransferItemSchema.product_id,
                to_outlet_expr.label("to_outlet_id"),
                func.sum(TransferItemSchema.quantity_delivered).label("total_received")
            )
            .join(StockTransferOrderSchema, TransferItemSchema.transfer_id == StockTransferOrderSchema.uid)
            .where(StockTransferOrderSchema.status == TransferStatus.DELIVERED)
            .group_by(TransferItemSchema.product_id, to_outlet_expr)
            .cte("transfer_in")
        )

        # 2. CTE: 'total_transferred_out' per product & from_outlet
        transfer_out_cte = (
            db.select(
                TransferItemSchema.product_id,
                from_outlet_expr.label("from_outlet_id"),
                func.sum(TransferItemSchema.quantity_delivered).label("total_transferred_out")
            )
            .join(StockTransferOrderSchema, TransferItemSchema.transfer_id == StockTransferOrderSchema.uid)
            .where(StockTransferOrderSchema.status == TransferStatus.DELIVERED)
            .group_by(TransferItemSchema.product_id, from_outlet_expr)
            .cte("transfer_out")
        )

        # 3. CTE: 'total_sold' per product & assigned_outlet
        orders_query_base = (
            db.select(
                OrderItemSchema.product_id,
                assigned_outlet_expr.label("assigned_outlet_id"),
                func.sum(OrderItemSchema.quantity).label("total_sold")
            )
            .join(CustomerOrderSchema, OrderItemSchema.order_id == CustomerOrderSchema.uid)
            .where(
                and_(
                    CustomerOrderSchema.order_status == OrderStatus.DELIVERED,
                    CustomerOrderSchema.actual_delivery_date.isnot(None)
                )
            )
        )
        
        if outlet_id is not None:
            if outlet_id.lower() == "null" or outlet_id == warehouse_id:
                orders_query_base = orders_query_base.where(
                    assigned_outlet_expr == warehouse_id
                )
            else:
                orders_query_base = orders_query_base.where(CustomerOrderSchema.assigned_outlet_id == outlet_id)
            
        if product_id:
            orders_query_base = orders_query_base.where(OrderItemSchema.product_id == product_id)
            
        orders_cte = orders_query_base.group_by(
            OrderItemSchema.product_id, 
            assigned_outlet_expr
        ).cte("orders_sold")

        # 4. Build the main query joining Inventory with the three CTEs
        stmt = (
            db.select(
                InventorySchema.uid,
                InventorySchema.product_id,
                InventorySchema.outlet_id,
                InventorySchema.quantity,
                InventorySchema.last_updated,
                func.coalesce(transfer_in_cte.c.total_received, 0).label("total_received"),
                func.coalesce(orders_cte.c.total_sold, 0).label("total_sold"),
                func.coalesce(transfer_out_cte.c.total_transferred_out, 0).label("total_transferred_out")
            )
            .outerjoin(
                transfer_in_cte,
                and_(
                    InventorySchema.product_id == transfer_in_cte.c.product_id,
                    InventorySchema.outlet_id == transfer_in_cte.c.to_outlet_id
                )
            )
            .outerjoin(
                transfer_out_cte,
                and_(
                    InventorySchema.product_id == transfer_out_cte.c.product_id,
                    InventorySchema.outlet_id == transfer_out_cte.c.from_outlet_id
                )
            )
            .outerjoin(
                orders_cte,
                and_(
                    InventorySchema.product_id == orders_cte.c.product_id,
                    InventorySchema.outlet_id == orders_cte.c.assigned_outlet_id
                )
            )
        )

        # 5. Apply Filters — never show factory outlets in inventory
        from managers import OutletSchema as _OutletSchema
        stmt = stmt.outerjoin(_OutletSchema, InventorySchema.outlet_id == _OutletSchema.uid).where(
            db.or_(
                InventorySchema.outlet_id.is_(None),
                _OutletSchema.outlet_type != OutletType.FACTORY
            )
        )

        if outlet_id is not None:
            if outlet_id.lower() == "null" or outlet_id == warehouse_id:
                stmt = stmt.where(InventorySchema.outlet_id == warehouse_id)
            else:
                stmt = stmt.where(InventorySchema.outlet_id == outlet_id)

        if scope_outlet is not None:
            stmt = stmt.where(InventorySchema.outlet_id.in_(scope_outlet)
                              if isinstance(scope_outlet, list)
                              else InventorySchema.outlet_id == scope_outlet)

        if product_id:
            stmt = stmt.where(InventorySchema.product_id == product_id)

        # 6. Execute the query using AsyncSession
        async with AsyncSession(engine) as session:
            # Get total count for pagination (simplified to avoid subquery complexities)
            count_stmt = db.select(func.count(InventorySchema.uid)).select_from(InventorySchema)
            if outlet_id is not None:
                if outlet_id.lower() == "null" or outlet_id == warehouse_id:
                    count_stmt = count_stmt.where(InventorySchema.outlet_id == warehouse_id)
                else:
                    count_stmt = count_stmt.where(InventorySchema.outlet_id == outlet_id)
            if scope_outlet is not None:
                count_stmt = count_stmt.where(InventorySchema.outlet_id.in_(scope_outlet)
                                              if isinstance(scope_outlet, list)
                                              else InventorySchema.outlet_id == scope_outlet)
            if product_id:
                count_stmt = count_stmt.where(InventorySchema.product_id == product_id)
                
            total_count = await session.scalar(count_stmt) or 0

            # Apply pagination
            if limit > 0:
                stmt = stmt.limit(limit).offset(offset)

            # Fetch results
            result = await session.execute(stmt)
            rows = result.all()

        # 7. Process Results
        inventory_responses = []
        for uid, p_id, o_id, qty, last_upd, total_received, total_sold, total_transferred_out in rows:
            # Recalculate quantity: Received - Sold - Transferred Out (cast to int for Pydantic)
            actual_quantity = int((total_received or 0) - (total_sold or 0) - (total_transferred_out or 0))
            
            delivered = int(total_sold or 0)
            display_received = int(total_received or 0)
            display_transferred_out = int(total_transferred_out or 0)

            available_quantity = max(0, actual_quantity)
            
            # Apply low stock filter dynamically if requested
            if low_stock_only:
                try:
                    product = await product_manager.fetch(p_id)
                    if available_quantity >= product.min_stock_level:
                        continue
                except:
                    continue

            inventory_responses.append(
                InventoryAuditResponse(
                    uid=uid,
                    product_id=p_id,
                    outlet_id=o_id,
                    quantity=int(qty or 0), # Use Physical Stock for display
                    db_quantity=int(qty or 0),
                    audited_quantity=actual_quantity, # Use Calculated Stock for audit
                    reserved_quantity=0,
                    available_quantity=int(qty or 0),
                    last_updated=last_upd or datetime.utcnow(),
                    total_received=display_received,
                    delivered=delivered,
                    total_transferred_out=display_transferred_out
                )
            )
            
        return ListResponse(items=inventory_responses, count=total_count)

    except Exception as e:
        import traceback
        traceback.print_exc()
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
    _: AuthContext = Depends(require_permission(Permission.INVENTORY_ADJUST)),
):
    """
    Manual stock adjustment with reason tracking
    Positive quantity_change = stock addition
    Negative quantity_change = stock reduction
    """
    try:
        # Normalize outlet_id: None or "null" becomes HASSAN_OUTLET_ID
        if payload.outlet_id is None or (isinstance(payload.outlet_id, str) and payload.outlet_id.lower() == "null"):
            payload.outlet_id = HASSAN_OUTLET_ID

        # Verify product exists
        product = await product_manager.fetch(payload.product_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
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
            
            # Reserved quantity check removed
            
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
    _: AuthContext = Depends(require_permission(Permission.INVENTORY_WRITE)),
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
        
        # Reservation logic removed. Stock is only deducted when delivered.
        return StatusResponse(
            status="ok",
            message=f"Reservation skipped. Stock will be deducted on delivery."
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
    _: AuthContext = Depends(require_permission(Permission.INVENTORY_WRITE)),
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
        
        # Reservation logic removed.
        return StatusResponse(
            status="ok",
            message=f"Release skipped. No stock was reserved."
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
    _: AuthContext = Depends(require_permission(Permission.INVENTORY_WRITE)),
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
        
        # Now simply consumes stock by decreasing quantity
        if inventory_item.quantity < quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient total stock. Available: {inventory_item.quantity}, Requested: {quantity}"
            )
        
        new_quantity = max(0, inventory_item.quantity - quantity)
        
        # Normalize outlet_id for update
        target_outlet_id = outlet_id
        if target_outlet_id is None or (isinstance(target_outlet_id, str) and target_outlet_id.lower() == "null"):
            target_outlet_id = HASSAN_OUTLET_ID
            
        await inventory_manager.update(
            inventory_item.uid,
            {
                "quantity": new_quantity,
                "outlet_id": target_outlet_id,
                "last_updated": datetime.utcnow()
            }
        )
        
        return StatusResponse(
            status="ok",
            message=f"Consumed {quantity} units. New quantity: {new_quantity}"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stock consumption failed: {str(e)}"
        )
