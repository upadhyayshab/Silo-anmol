from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional
from datetime import datetime, date
from decimal import Decimal

from config import get_settings, get_engine
from managers import (
    CustomerOrderManager, OrderItemManager, OrderTransactionManager,
    InventoryManager, ProductManager, OutletManager, UserManager,
    CustomerOrderSchema, OrderItemSchema, OrderTransactionSchema
)
from models import (
    OrderCreateRequest, OrderUpdateRequest, OrderStatusUpdateRequest,
    OrderAssignRequest, OrderTransactionCreateRequest, PaymentStatusUpdateRequest,
    OrderResponse, OrderTransactionResponse,
    ListResponse, StatusResponse
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, OrderStatus, PaymentStatus, CollectionType
import uuid

settings = get_settings()
engine = get_engine(settings.name)

order_manager = CustomerOrderManager(engine)
order_item_manager = OrderItemManager(engine)
transaction_manager = OrderTransactionManager(engine)
inventory_manager = InventoryManager(engine)
product_manager = ProductManager(engine)
outlet_manager = OutletManager(engine)
user_manager = UserManager(engine)

router = APIRouter(prefix="/orders", tags=["Order Management"])


def generate_order_number() -> str:
    """Generate unique order number"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"ORD-{timestamp}-{str(uuid.uuid4())[:8].upper()}"


@router.post("", response_model=OrderResponse)
async def create_order(
    payload: OrderCreateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.TELECALLER, UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Create new customer order
    - Telecallers: Create orders for phone/online customers
    - Outlet Managers: Create orders for walk-in customers at their outlet
    Automatically reserves stock at assigned outlet
    """
    try:
        # Validate products and calculate total
        total_amount = Decimal('0.00')
        validated_items = []
        
        for item in payload.items:
            # Verify product exists and is active
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
            
            # Calculate item subtotal
            subtotal = item.quantity * item.unit_price
            total_amount += subtotal
            
            validated_items.append({
                "product": product,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "subtotal": subtotal
            })
        
        # Create order
        order_number = generate_order_number()
        
        # Get current user to determine order assignment
        current_user = await user_manager.fetch(current_user_id)
        
        # For outlet managers, auto-assign to their outlet
        assigned_outlet_id = None
        if current_user.role == UserRole.OUTLET_MANAGER and current_user.outlet_id:
            assigned_outlet_id = current_user.outlet_id
        
        new_order = CustomerOrderSchema(
            order_number=order_number,
            customer_name=payload.customer_name,
            customer_phone=payload.customer_phone,
            house_no=payload.house_no,
            street=payload.street,
            address_line=payload.address_line,
            village=payload.village,
            taluk=payload.taluk,
            district=payload.district,
            state=payload.state,
            pincode=payload.pincode,
            telecaller_id=current_user_id,
            assigned_outlet_id=assigned_outlet_id,
            order_status=OrderStatus.PENDING,
            collection_type=payload.collection_type,
            payment_method=payload.payment_method,
            order_date=datetime.utcnow(),
            expected_delivery_date=payload.expected_delivery_date,
            total_amount=total_amount
        )
        
        created_order = await order_manager.create(new_order)
        
        # Create order items
        order_items = []
        for item_data in validated_items:
            order_item = OrderItemSchema(
                order_id=created_order.uid,
                product_id=item_data["product"].uid,
                quantity=item_data["quantity"],
                unit_price=item_data["unit_price"],
                subtotal=item_data["subtotal"]
            )
            created_item = await order_item_manager.create(order_item)
            order_items.append(created_item)
        
        # Auto-assign outlet based on delivery area (for telecaller orders)
        # Outlet manager orders are already assigned to their outlet
        if not assigned_outlet_id:
            assigned_outlet = await auto_assign_outlet(payload.district, payload.state)
            if assigned_outlet:
                # Update order with assigned outlet
                await order_manager.update(
                    created_order.uid,
                    {"assigned_outlet_id": assigned_outlet.uid}
                )
                created_order.assigned_outlet_id = assigned_outlet.uid
                assigned_outlet_id = assigned_outlet.uid
        
        # Reserve stock at assigned outlet
        if assigned_outlet_id:
            try:
                await reserve_order_stock(created_order.uid, assigned_outlet_id, validated_items)
            except Exception as e:
                # If stock reservation fails, keep order as pending
                await order_manager.update(
                    created_order.uid,
                    {
                        "status_remarks": f"Stock reservation failed: {str(e)}",
                        "order_status": OrderStatus.PENDING
                    }
                )
        
        # Fetch complete order with items
        return await get_order_response(created_order.uid)
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create order: {str(e)}"
        )


async def auto_assign_outlet(district: str, state: str) -> Optional[object]:
    """Auto-assign outlet based on delivery area"""
    try:
        # Simple logic: find outlet in same district/state
        outlets = await outlet_manager.fetch_all(
            filters={"is_active": True}
        )
        
        # First try: exact district match
        for outlet in outlets.items:
            if outlet.city.lower() == district.lower() and outlet.state.lower() == state.lower():
                return outlet
        
        # Second try: same state
        for outlet in outlets.items:
            if outlet.state.lower() == state.lower():
                return outlet
        
        # Fallback: first active outlet
        if outlets.items:
            return outlets.items[0]
        
        return None
    except:
        return None


async def reserve_order_stock(order_id: str, outlet_id: str, validated_items: List[dict]):
    """Reserve stock for order items at assigned outlet"""
    for item_data in validated_items:
        product_id = item_data["product"].uid
        quantity = item_data["quantity"]
        
        # Find inventory at outlet
        inventory_items = await inventory_manager.fetch_all(
            filters={
                "product_id": product_id,
                "outlet_id": outlet_id
            }
        )
        
        if not inventory_items.items:
            # Try warehouse stock
            warehouse_inventory = await inventory_manager.fetch_all(
                filters={
                    "product_id": product_id,
                    "outlet_id": None
                }
            )
            if not warehouse_inventory.items:
                raise Exception(f"No stock available for product {item_data['product'].product_name}")
            
            inventory_item = warehouse_inventory.items[0]
        else:
            inventory_item = inventory_items.items[0]
        
        # Check available stock
        available = inventory_item.quantity - inventory_item.reserved_quantity
        if available < quantity:
            raise Exception(f"Insufficient stock for {item_data['product'].product_name}. Available: {available}, Required: {quantity}")
        
        # Reserve stock
        new_reserved = inventory_item.reserved_quantity + quantity
        await inventory_manager.update(
            inventory_item.uid,
            {
                "reserved_quantity": new_reserved,
                "last_updated": datetime.utcnow()
            }
        )


# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    current_user_id: str = Depends(require_roles(
        UserRole.TELECALLER, UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Get specific order details"""
    try:
        order = await order_manager.fetch(order_id)
        
        # Check access permissions
        current_user = await user_manager.fetch(current_user_id)
        
        # Role-based access control
        if current_user.role == UserRole.TELECALLER:
            if order.telecaller_id != current_user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view your own orders"
                )
        elif current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id and order.outlet_id != current_user.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view orders for your outlet"
                )
        
        # Get order items
        order_items = await order_item_manager.fetch_all(
            filters={"order_id": order_id}
        )
        
        # Build response
        return OrderResponse(
            uid=order.uid,
            order_number=order.order_number,
            customer_name=order.customer_name,
            customer_phone=order.customer_phone,
            customer_address=order.customer_address,
            telecaller_id=order.telecaller_id,
            outlet_id=order.outlet_id,
            status=order.status,
            payment_status=order.payment_status,
            total_amount=order.total_amount,
            advance_amount=order.advance_amount,
            delivery_date=order.delivery_date,
            notes=order.notes,
            items=[
                {
                    "product_id": item.product_id,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "total_price": item.total_price
                }
                for item in order_items.items
            ],
            created_at=order.created_at,
            created_by=order.created_by,
            last_updated=order.last_updated
        )
    
    except Exception as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch order: {str(e)}"
        )


# Duplicate GET /{order_id}/transactions route removed - moved to top of file
    """Get all transactions for an order"""
    try:
        # Verify order exists and user has access
        order = await order_manager.fetch(order_id)
        
        current_user = await user_manager.fetch(current_user_id)
        
        # Role-based access control
        if current_user.role == UserRole.TELECALLER:
            if order.telecaller_id != current_user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view transactions for your own orders"
                )
        elif current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id and order.outlet_id != current_user.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view transactions for your outlet's orders"
                )
        
        # Get transactions
        transactions = await transaction_manager.fetch_all(
            filters={"order_id": order_id}
        )
        
        transaction_responses = []
        for transaction in transactions.items:
            transaction_responses.append(OrderTransactionResponse(
                uid=transaction.uid,
                order_id=transaction.order_id,
                amount=transaction.amount,
                payment_method=transaction.payment_method,
                collection_type=transaction.collection_type,
                reference_number=transaction.reference_number,
                notes=transaction.notes,
                created_at=transaction.created_at,
                created_by=transaction.created_by
            ))
        
        return ListResponse(items=transaction_responses, count=len(transaction_responses))
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch order transactions: {str(e)}"
        )


# GENERIC ROUTE LAST (after all specific routes)

@router.get("", response_model=ListResponse[OrderResponse])
async def get_orders(transfer_status: Optional[OrderStatus] = None,
    telecaller_id: Optional[str] = None,
    outlet_id: Optional[str] = None,
    customer_phone: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    limit: int = 50,
    offset: int = 0,
    current_user_id: str = Depends(require_roles(
        UserRole.TELECALLER, UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Get orders with filters
    Telecallers see only their orders, managers see outlet orders
    """
    try:
        # Get current user to determine access level
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {}
        
        # Role-based filtering
        if current_user.role == UserRole.TELECALLER:
            filters["telecaller_id"] = current_user_id
        elif current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id:
                filters["assigned_outlet_id"] = current_user.outlet_id
        
        # Apply additional filters
        if transfer_status:
            filters["order_status"] = status
        if telecaller_id and current_user.role in [UserRole.ADMIN, UserRole.SUPER_ADMIN]:
            filters["telecaller_id"] = telecaller_id
        if outlet_id and current_user.role in [UserRole.ADMIN, UserRole.SUPER_ADMIN]:
            filters["assigned_outlet_id"] = outlet_id
        if customer_phone:
            filters["customer_phone"] = customer_phone
        
        orders = await order_manager.fetch_all(
            filters=filters,
            limit=limit,
            offset=offset
        )
        
        order_responses = []
        for order in orders.items:
            # Filter by date range if specified
            if from_date and order.order_date.date() < from_date:
                continue
            if to_date and order.order_date.date() > to_date:
                continue
            
            order_response = await get_order_response(order.uid)
            order_responses.append(order_response)
        
        return ListResponse(items=order_responses, count=len(order_responses))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch orders: {str(e)}"
        )


# Duplicate GET /{order_id} route removed - moved to top of file
    """Get specific order details"""
    try:
        order = await order_manager.fetch(order_id)
        
        # Check access permissions
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.TELECALLER and order.telecaller_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: You can only view your own orders"
            )
        elif current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id != order.assigned_outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view orders assigned to your outlet"
                )
        
        return await get_order_response(order_id)
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )


async def get_order_response(order_id: str) -> OrderResponse:
    """Helper to build complete order response with items"""
    order = await order_manager.fetch(order_id)
    
    # Get order items
    order_items = await order_item_manager.fetch_all(
        filters={"order_id": order_id}
    )
    
    from models import OrderItemResponse
    items = [
        OrderItemResponse(
            uid=item.uid,
            product_id=item.product_id,
            quantity=item.quantity,
            unit_price=item.unit_price,
            subtotal=item.subtotal
        )
        for item in order_items.items
    ]
    
    return OrderResponse(
        uid=order.uid,
        order_number=order.order_number,
        customer_name=order.customer_name,
        customer_phone=order.customer_phone,
        address_line=order.address_line,
        district=order.district,
        state=order.state,
        pincode=order.pincode,
        telecaller_id=order.telecaller_id,
        assigned_outlet_id=order.assigned_outlet_id,
        order_status=order.order_status,
        collection_type=order.collection_type,
        payment_method=order.payment_method,
        order_date=order.order_date,
        expected_delivery_date=order.expected_delivery_date,
        actual_delivery_date=order.actual_delivery_date,
        status_remarks=order.status_remarks,
        total_amount=order.total_amount,
        items=items,
        created_at=order.created_at
    )


@router.put("/{order_id}", response_model=OrderResponse)
async def update_order(
    order_id: str,
    payload: OrderUpdateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.TELECALLER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Update order details (only for pending orders)"""
    try:
        order = await order_manager.fetch(order_id)
        
        # Check permissions
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.TELECALLER and order.telecaller_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: You can only update your own orders"
            )
        
        # Only allow updates for pending orders
        if order.order_status != OrderStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Can only update pending orders"
            )
        
        # Prepare update data
        update_data = {}
        if payload.customer_name:
            update_data["customer_name"] = payload.customer_name
        if payload.customer_phone:
            update_data["customer_phone"] = payload.customer_phone
        if payload.address_line:
            update_data["address_line"] = payload.address_line
        if payload.district:
            update_data["district"] = payload.district
        if payload.state:
            update_data["state"] = payload.state
        if payload.pincode:
            update_data["pincode"] = payload.pincode
        if payload.expected_delivery_date:
            update_data["expected_delivery_date"] = payload.expected_delivery_date
        
        if update_data:
            await order_manager.update(order_id, update_data)
        
        return await get_order_response(order_id)
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update order: {str(e)}"
        )


@router.put("/{order_id}/status", response_model=StatusResponse)
async def update_order_status(
    order_id: str,
    payload: OrderStatusUpdateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Update order status (Outlet Manager function)"""
    try:
        order = await order_manager.fetch(order_id)
        
        # Check permissions
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id != order.assigned_outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only update orders assigned to your outlet"
                )
        
        # Validate status transition
        if not is_valid_status_transition(order.order_status, payload.order_status):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status transition from {order.order_status} to {payload.order_status}"
            )
        
        # Handle status-specific logic
        update_data = {
            "order_status": payload.order_status,
            "status_remarks": payload.status_remarks
        }
        
        if payload.order_status == OrderStatus.DELIVERED:
            update_data["actual_delivery_date"] = datetime.utcnow()
            # Consume reserved stock
            await consume_order_stock(order_id)
        
        elif payload.order_status == OrderStatus.CANCELLED:
            if not payload.status_remarks:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Status remarks required for cancelled orders"
                )
            # Release reserved stock
            await release_order_stock(order_id)
        
        await order_manager.update(order_id, update_data)
        
        return StatusResponse(
            status="ok",
            message=f"Order status updated to {payload.order_status}"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update order status: {str(e)}"
        )


def is_valid_status_transition(current_status: OrderStatus, new_status: OrderStatus) -> bool:
    """Validate order status transitions"""
    valid_transitions = {
        OrderStatus.PENDING: [OrderStatus.DELIVERY_ALLOTTED, OrderStatus.CANCELLED],
        OrderStatus.DELIVERY_ALLOTTED: [OrderStatus.DELIVERED, OrderStatus.CANCELLED],
        OrderStatus.DELIVERED: [],  # Final state
        OrderStatus.CANCELLED: []   # Final state
    }
    
    return new_status in valid_transitions.get(current_status, [])


async def consume_order_stock(order_id: str):
    """Consume reserved stock when order is delivered"""
    order = await order_manager.fetch(order_id)
    order_items = await order_item_manager.fetch_all(
        filters={"order_id": order_id}
    )
    
    for item in order_items.items:
        # Find inventory record
        inventory_items = await inventory_manager.fetch_all(
            filters={
                "product_id": item.product_id,
                "outlet_id": order.assigned_outlet_id
            }
        )
        
        if not inventory_items.items:
            # Try warehouse
            inventory_items = await inventory_manager.fetch_all(
                filters={
                    "product_id": item.product_id,
                    "outlet_id": None
                }
            )
        
        if inventory_items.items:
            inventory_item = inventory_items.items[0]
            new_quantity = inventory_item.quantity - item.quantity
            new_reserved = inventory_item.reserved_quantity - item.quantity
            
            await inventory_manager.update(
                inventory_item.uid,
                {
                    "quantity": max(0, new_quantity),
                    "reserved_quantity": max(0, new_reserved),
                    "last_updated": datetime.utcnow()
                }
            )


async def release_order_stock(order_id: str):
    """Release reserved stock when order is cancelled"""
    order = await order_manager.fetch(order_id)
    order_items = await order_item_manager.fetch_all(
        filters={"order_id": order_id}
    )
    
    for item in order_items.items:
        # Find inventory record
        inventory_items = await inventory_manager.fetch_all(
            filters={
                "product_id": item.product_id,
                "outlet_id": order.assigned_outlet_id
            }
        )
        
        if not inventory_items.items:
            # Try warehouse
            inventory_items = await inventory_manager.fetch_all(
                filters={
                    "product_id": item.product_id,
                    "outlet_id": None
                }
            )
        
        if inventory_items.items:
            inventory_item = inventory_items.items[0]
            new_reserved = inventory_item.reserved_quantity - item.quantity
            
            await inventory_manager.update(
                inventory_item.uid,
                {
                    "reserved_quantity": max(0, new_reserved),
                    "last_updated": datetime.utcnow()
                }
            )


@router.put("/{order_id}/assign", response_model=StatusResponse)
async def assign_order_to_outlet(
    order_id: str,
    payload: OrderAssignRequest,
    _: str = Depends(require_roles(UserRole.ADMIN, UserRole.SUPER_ADMIN))
):
    """Manually assign order to outlet (Admin function)"""
    try:
        order = await order_manager.fetch(order_id)
        
        if order.order_status != OrderStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Can only assign pending orders"
            )
        
        # Verify outlet exists
        try:
            outlet = await outlet_manager.fetch(payload.assigned_outlet_id)
            if not outlet.is_active:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot assign to inactive outlet"
                )
        except:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Outlet not found"
            )
        
        # Release stock from old outlet if assigned
        if order.assigned_outlet_id:
            await release_order_stock(order_id)
        
        # Update assignment
        await order_manager.update(
            order_id,
            {"assigned_outlet_id": payload.assigned_outlet_id}
        )
        
        # Reserve stock at new outlet
        order_items = await order_item_manager.fetch_all(
            filters={"order_id": order_id}
        )
        
        validated_items = []
        for item in order_items.items:
            product = await product_manager.fetch(item.product_id)
            validated_items.append({
                "product": product,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "subtotal": item.subtotal
            })
        
        try:
            await reserve_order_stock(order_id, payload.assigned_outlet_id, validated_items)
        except Exception as e:
            await order_manager.update(
                order_id,
                {"status_remarks": f"Stock reservation failed: {str(e)}"}
            )
        
        return StatusResponse(
            status="ok",
            message=f"Order assigned to outlet {outlet.outlet_name}"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to assign order: {str(e)}"
        )


@router.post("/{order_id}/transactions", response_model=OrderTransactionResponse)
async def add_order_transaction(
    order_id: str,
    payload: OrderTransactionCreateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Add payment transaction to order"""
    try:
        order = await order_manager.fetch(order_id)
        
        # Check permissions
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id != order.assigned_outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only add transactions for orders assigned to your outlet"
                )
        
        # Create transaction
        transaction = OrderTransactionSchema(
            order_id=order_id,
            payment_status=PaymentStatus.PAID,
            payment_method=payload.payment_method,
            amount_paid=payload.amount_paid,
            transaction_reference=payload.transaction_reference,
            payment_date=datetime.utcnow(),
            received_by=current_user_id,
            notes=payload.notes
        )
        
        created_transaction = await transaction_manager.create(transaction)
        
        return OrderTransactionResponse(
            uid=created_transaction.uid,
            order_id=created_transaction.order_id,
            payment_status=created_transaction.payment_status,
            payment_method=created_transaction.payment_method,
            amount_paid=created_transaction.amount_paid,
            transaction_reference=created_transaction.transaction_reference,
            payment_date=created_transaction.payment_date,
            received_by=created_transaction.received_by,
            notes=created_transaction.notes
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to add transaction: {str(e)}"
        )


# Duplicate GET /{order_id}/transactions route removed - moved to top of file
    """Get all transactions for an order"""
    try:
        order = await order_manager.fetch(order_id)
        
        # Check permissions
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.TELECALLER and order.telecaller_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: You can only view transactions for your own orders"
            )
        elif current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id != order.assigned_outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view transactions for orders assigned to your outlet"
                )
        
        transactions = await transaction_manager.fetch_all(
            filters={"order_id": order_id}
        )
        
        transaction_responses = [
            OrderTransactionResponse(
                uid=txn.uid,
                order_id=txn.order_id,
                payment_status=txn.payment_status,
                payment_method=txn.payment_method,
                amount_paid=txn.amount_paid,
                transaction_reference=txn.transaction_reference,
                payment_date=txn.payment_date,
                received_by=txn.received_by,
                notes=txn.notes
            )
            for txn in transactions.items
        ]
        
        return ListResponse(items=transaction_responses, count=len(transaction_responses))
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch transactions: {str(e)}"
        )


@router.put("/{order_id}/payment-status", response_model=StatusResponse)
async def update_order_payment_status(
    order_id: str,
    payload: PaymentStatusUpdateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER
    ))
):
    """
    Update payment status of an order
    Used to mark orders as paid, partially paid, etc.
    """
    try:
        # Verify order exists and check access
        order = await order_manager.fetch(order_id)
        
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER:
            if order.assigned_outlet_id != current_user.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this order"
                )
        
        # Create a status update transaction record
        transaction = OrderTransactionSchema(
            order_id=order_id,
            payment_status=payload.payment_status,
            payment_method=order.payment_method,  # Use order's payment method
            amount_paid=0.00,  # Status update, not actual payment
            transaction_reference=f"STATUS_UPDATE_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            payment_date=datetime.now(),
            received_by=current_user_id,
            notes=f"Payment status updated to {payload.payment_status.value}. {payload.notes or ''}"
        )
        
        await transaction_manager.create(transaction)
        
        # If marked as delivered and paid, update order status
        if payload.payment_status == PaymentStatus.PAID:
            await order_manager.update(order_id, {
                "order_status": OrderStatus.DELIVERED,
                "actual_delivery_date": datetime.now()
            })
        
        return StatusResponse(
            status="ok",
            message=f"Order payment status updated to {payload.payment_status.value}"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update payment status: {str(e)}"
        )