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
    OrderAssignRequest, OrderRevokeRequest, OrderTransactionCreateRequest, PaymentStatusUpdateRequest,
    OrderResponse, OrderItemResponse, OrderTransactionResponse,
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
        # DEBUG: Log the received prepaid amount with type information
        print(f"🔍 DEBUG: Received prepaid_amount = {payload.prepaid_amount} (type: {type(payload.prepaid_amount)})")
        print(f"🔍 DEBUG: Received manual_discount = {payload.manual_discount} (type: {type(payload.manual_discount)})")
        print(f"🔍 DEBUG: Full payload: {payload}")
        
        # Additional debug: Check if there's any conversion happening
        original_prepaid = payload.prepaid_amount
        print(f"🔍 DEBUG: original_prepaid variable = {original_prepaid} (type: {type(original_prepaid)})")
        
        # Validate products and calculate pricing
        gross_amount = Decimal('0.00')  # Total at MRP (cost_price)
        product_discount_total = Decimal('0.00')  # Sum of all product manual discounts
        total_commission = Decimal('0.00')  # Total commission for the order
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
            
            # Calculate gross amount (always at MRP/cost_price)
            item_gross = item.quantity * product.cost_price
            gross_amount += item_gross
            
            # Calculate product manual discount total
            product_discount_total += item.product_manual_discount
            
            # Calculate unit_price after product manual discount
            per_unit_discount = item.product_manual_discount / item.quantity if item.quantity > 0 else Decimal('0.00')
            calculated_unit_price = product.cost_price - per_unit_discount
            # Ensure unit_price is not negative
            if calculated_unit_price < 0:
                calculated_unit_price = Decimal('0.00')
            
            # Calculate item subtotal
            subtotal = item.quantity * calculated_unit_price
            
            # Calculate commission for this item
            # Commission base = cost_price - per_unit_discount
            commission_base = max(product.cost_price - per_unit_discount, Decimal('0.00'))
            # Commission percentage interpretation (product.commission is now percentage 0-100)
            commission_per_unit = commission_base * (product.commission / 100)
            item_commission = commission_per_unit * item.quantity
            total_commission += item_commission
            
            validated_items.append({
                "product": product,
                "quantity": item.quantity,
                "unit_price": calculated_unit_price,
                "subtotal": subtotal,
                "gross_amount": item_gross,
                "product_manual_discount": item.product_manual_discount,
                "commission": item_commission
            })
        
        # Calculate final pricing (simplified - no manual_discount condition)
        discount_applied = product_discount_total  # Always use product manual discounts
        amount_after_discount = gross_amount - product_discount_total
        
        # Apply prepaid amount
        final_total_amount = amount_after_discount - payload.prepaid_amount
        
        # Validation checks
        if product_discount_total > gross_amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Total product discounts ({product_discount_total}) cannot exceed gross amount ({gross_amount})"
            )
        
        if payload.prepaid_amount > amount_after_discount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Prepaid amount ({payload.prepaid_amount}) cannot exceed order total after discount ({amount_after_discount})"
            )
        
        if final_total_amount < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Final order amount cannot be negative"
            )
        
        # Create order
        order_number = generate_order_number()
        
        # Get current user to determine order assignment
        current_user = await user_manager.fetch(current_user_id)
        
        # For outlet managers, auto-assign to their outlet
        assigned_outlet_id = None
        if current_user.role == UserRole.OUTLET_MANAGER and current_user.outlet_id:
            assigned_outlet_id = current_user.outlet_id
        
        # DEBUG: Log values before creating order with detailed type information
        print(f"🔍 DEBUG: Before creating order:")
        print(f"   • gross_amount = {gross_amount} (type: {type(gross_amount)})")
        print(f"   • manual_discount = {payload.manual_discount} (type: {type(payload.manual_discount)}) [LEGACY - IGNORED]")
        print(f"   • discount_applied = {discount_applied} (type: {type(discount_applied)})")
        print(f"   • prepaid_amount = {payload.prepaid_amount} (type: {type(payload.prepaid_amount)})")
        print(f"   • total_commission = {total_commission} (type: {type(total_commission)})")
        print(f"   • final_total_amount = {final_total_amount} (type: {type(final_total_amount)})")
        
        # Additional check: Verify the prepaid amount hasn't changed
        if payload.prepaid_amount != original_prepaid:
            print(f"❌ CRITICAL: prepaid_amount changed from {original_prepaid} to {payload.prepaid_amount}!")
        
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
            gross_amount=gross_amount,
            manual_discount=payload.manual_discount,  # Keep for backward compatibility but not used in calculations
            discount_applied=discount_applied,
            prepaid_amount=payload.prepaid_amount,
            total_amount=final_total_amount,
            total_commission=total_commission  # New field
        )
        
        # DEBUG: Log the created order schema values
        print(f"🔍 DEBUG: Created order schema:")
        print(f"   • new_order.prepaid_amount = {new_order.prepaid_amount}")
        print(f"   • new_order.manual_discount = {new_order.manual_discount}")
        print(f"   • new_order.total_amount = {new_order.total_amount}")
        
        created_order = await order_manager.create(new_order)
        
        # DEBUG: Log the created order from database
        print(f"🔍 DEBUG: After database insert:")
        print(f"   • created_order.prepaid_amount = {created_order.prepaid_amount}")
        print(f"   • created_order.manual_discount = {created_order.manual_discount}")
        print(f"   • created_order.total_amount = {created_order.total_amount}")
        print(f"   • created_order.total_commission = {created_order.total_commission}")
        
        # Create order items with detailed error handling
        order_items = []
        items_creation_errors = []
        
        for i, item_data in enumerate(validated_items):
            try:
                order_item = OrderItemSchema(
                    order_id=created_order.uid,
                    product_id=item_data["product"].uid,
                    quantity=item_data["quantity"],
                    unit_price=item_data["unit_price"],
                    total_price=item_data["subtotal"],  # Use subtotal as total_price for database
                    subtotal=item_data["subtotal"],
                    product_manual_discount=item_data["product_manual_discount"]  # New field
                )
                created_item = await order_item_manager.create(order_item)
                order_items.append(created_item)
            except Exception as item_error:
                error_msg = f"Failed to create item {i+1} (product: {item_data['product'].product_name}): {str(item_error)}"
                items_creation_errors.append(error_msg)
                print(f"❌ ORDER ITEM CREATION ERROR: {error_msg}")  # Debug logging
        
        # If no items were created, this is a critical error
        if not order_items and validated_items:
            error_details = "; ".join(items_creation_errors) if items_creation_errors else "Unknown error in items creation"
            # Update order with error status
            await order_manager.update(
                created_order.uid,
                {
                    "status_remarks": f"Order created but items creation failed: {error_details}",
                    "order_status": OrderStatus.PENDING
                }
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Order created but failed to create order items: {error_details}"
            )
        
        # Auto-assign outlet based on delivery area (for telecaller orders)
        # Outlet manager orders are already assigned to their outlet
        if not assigned_outlet_id:
            assigned_outlet = await auto_assign_outlet(payload.district, payload.state, payload.taluk)
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
        
        # Build response directly to avoid potential SQLAlchemy session issues
        order_items_response = []
        for item in order_items:
            try:
                order_items_response.append(OrderItemResponse(
                    uid=item.uid,
                    product_id=item.product_id,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    subtotal=item.subtotal,  # Use subtotal field for response
                    product_manual_discount=getattr(item, 'product_manual_discount', Decimal('0.00'))  # New field with backward compatibility
                ))
            except Exception as response_error:
                print(f"❌ ORDER ITEM RESPONSE ERROR: {str(response_error)}")  # Debug logging
        
        # Build the order response with proper error handling
        try:
            return OrderResponse(
                uid=created_order.uid,
                order_number=created_order.order_number,
                customer_name=created_order.customer_name,
                customer_phone=created_order.customer_phone,
                house_no=created_order.house_no,
                street=created_order.street,
                address_line=created_order.address_line,
                village=created_order.village,
                taluk=created_order.taluk,
                district=created_order.district,
                state=created_order.state,
                pincode=created_order.pincode,
                telecaller_id=created_order.telecaller_id,
                assigned_outlet_id=assigned_outlet_id,
                order_status=created_order.order_status,
                collection_type=created_order.collection_type,
                payment_method=created_order.payment_method,
                order_date=created_order.order_date,
                expected_delivery_date=created_order.expected_delivery_date,
                actual_delivery_date=created_order.actual_delivery_date,
                status_remarks=created_order.status_remarks,
                gross_amount=gross_amount,
                manual_discount=payload.manual_discount,  # Keep for backward compatibility
                discount_applied=discount_applied,
                prepaid_amount=created_order.prepaid_amount,
                total_amount=final_total_amount,
                total_commission=total_commission,  # New field
                items=order_items_response,
                created_at=created_order.created_at
            )
        except Exception as response_error:
            print(f"❌ ORDER RESPONSE CONSTRUCTION ERROR: {str(response_error)}")  # Debug logging
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Order created successfully but failed to build response: {str(response_error)}"
            )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create order: {str(e)}"
        )


async def auto_assign_outlet(district: str, state: str, taluk: str = None) -> Optional[object]:
    """Auto-assign outlet based on Google Sheets mapping"""
    try:
        import httpx
        
        # Google Apps Script endpoint for outlet mapping
        GOOGLE_OUTLET_API = "https://script.google.com/macros/s/AKfycbxqlS7Og-4AKNm79aweOzjVqOcmyFXHaHpy7xmfcz27i0knowG_vjEFWZ_Ha8drY4CYbA/exec"
        
        # Prepare API request parameters
        params = {
            "action": "getOutlet",
            "district": district,
            "taluk": taluk or ""  # Use empty string if taluk is None
        }
        
        print(f"🔍 DEBUG: Calling Google Outlet API with district='{district}', taluk='{taluk}'")
        
        # Call Google Apps Script API with redirect following
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(GOOGLE_OUTLET_API, params=params)
            
            print(f"📡 DEBUG: API response status: {response.status_code}")
            print(f"📡 DEBUG: Final URL: {response.url}")
            
            if response.status_code != 200:
                print(f"❌ Google API error: {response.status_code}")
                print(f"❌ Response: {response.text[:200]}...")
                return None
            
            try:
                api_data = response.json()
                print(f"📋 Google API response: {api_data}")
            except Exception as json_error:
                print(f"❌ JSON parsing error: {json_error}")
                print(f"❌ Raw response: {response.text[:200]}...")
                return None
            
            if not api_data.get("success"):
                print(f"❌ Google API returned success=false")
                return None
            
            # Get outlet name from API response
            api_outlet_name = api_data.get("outlet", "").strip()
            if not api_outlet_name:
                print(f"❌ No outlet name in API response")
                return None
            
            print(f"🎯 API returned outlet: '{api_outlet_name}'")
            
            # Get all active outlets from database
            outlets = await outlet_manager.fetch_all(
                filters={"is_active": True}
            )
            
            if not outlets.items:
                print(f"❌ No active outlets found in database")
                return None
            
            # Match outlet using first word comparison (case-insensitive)
            api_first_word = api_outlet_name.lower().split()[0] if api_outlet_name else ""
            
            print(f"🔍 Looking for outlets matching first word: '{api_first_word}'")
            
            matched_outlet = None
            for outlet in outlets.items:
                outlet_first_word = outlet.outlet_name.lower().split()[0] if outlet.outlet_name else ""
                print(f"   • Checking '{outlet.outlet_name}' (first word: '{outlet_first_word}')")
                
                if api_first_word == outlet_first_word:
                    matched_outlet = outlet
                    print(f"✅ MATCH FOUND: '{outlet.outlet_name}' matches API response '{api_outlet_name}'")
                    break
            
            if matched_outlet:
                return matched_outlet
            
            # Fallback: If no exact match, log available outlets and return None
            print(f"❌ No outlet found matching '{api_outlet_name}' (first word: '{api_first_word}')")
            print(f"📋 Available outlet first words: {[o.outlet_name.lower().split()[0] for o in outlets.items]}")
            print(f"💡 SUGGESTION: Update Google Sheets to return one of these outlet names:")
            for outlet in outlets.items:
                print(f"   • '{outlet.outlet_name}' (use '{outlet.outlet_name.split()[0]}' in Google Sheets)")
            
            return None
            
    except Exception as e:
        print(f"❌ Error in Google outlet assignment: {str(e)}")
        import traceback
        traceback.print_exc()
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
            if current_user.outlet_id and order.assigned_outlet_id != current_user.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view orders for your outlet"
                )
        
        # Get order items
        order_items = await order_item_manager.fetch_all(
            filters={"order_id": order_id}
        )
        
        # Use the helper function to build proper response
        return await get_order_response(order_id)
    
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


@router.get("/by-phone/{phone}", response_model=ListResponse[OrderResponse])
async def get_orders_by_phone(
    phone: str,
    limit: int = 50,
    offset: int = 0,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER,
        UserRole.OUTLET_MANAGER, UserRole.TELECALLER, UserRole.ACCOUNTANT
    ))
):
    """
    Get all orders for a customer by phone number
    
    Internal use - all staff can access for customer service
    Returns all orders regardless of telecaller or outlet assignment
    
    Args:
        phone: Customer phone number (10 digits)
        limit: Maximum number of orders to return (default: 50)
        offset: Pagination offset (default: 0)
    
    Returns:
        List of orders with full details including order items
    """
    try:
        # Validate and sanitize phone number
        sanitized_phone = phone.strip().replace(" ", "").replace("-", "").replace("+91", "")
        
        # Basic validation: should be 10 digits
        if not sanitized_phone.isdigit() or len(sanitized_phone) != 10:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid phone number format. Expected 10 digits."
            )
        
        # Query orders by phone number (no role-based filtering)
        orders = await order_manager.fetch_all(
            filters={"customer_phone": sanitized_phone},
            limit=limit,
            offset=offset
        )
        
        # Build complete order responses with items
        order_responses = []
        for order in orders.items:
            order_response = await get_order_response(order.uid)
            order_responses.append(order_response)
        
        # Sort by order_date descending (newest first)
        order_responses.sort(key=lambda x: x.order_date, reverse=True)
        
        return ListResponse(items=order_responses, count=len(order_responses))
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch orders by phone: {str(e)}"
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
            subtotal=item.subtotal,
            product_manual_discount=getattr(item, 'product_manual_discount', Decimal('0.00'))  # New field with backward compatibility
        )
        for item in order_items.items
    ]
    
    # Handle backward compatibility for orders created before new pricing fields
    gross_amount = getattr(order, 'gross_amount', order.total_amount)
    manual_discount = getattr(order, 'manual_discount', Decimal('0.00'))
    discount_applied = getattr(order, 'discount_applied', Decimal('0.00'))
    prepaid_amount = getattr(order, 'prepaid_amount', Decimal('0.00'))
    total_commission = getattr(order, 'total_commission', Decimal('0.00'))  # New field with backward compatibility
    
    return OrderResponse(
        uid=order.uid,
        order_number=order.order_number,
        customer_name=order.customer_name,
        customer_phone=order.customer_phone,
        house_no=getattr(order, 'house_no', None),
        street=getattr(order, 'street', None),
        address_line=order.address_line,
        village=getattr(order, 'village', None),
        taluk=getattr(order, 'taluk', None),
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
        gross_amount=gross_amount,
        manual_discount=manual_discount,
        discount_applied=discount_applied,
        prepaid_amount=prepaid_amount,
        total_amount=order.total_amount,
        total_commission=total_commission,  # New field
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



@router.delete("/{order_id}", response_model=StatusResponse)
async def delete_order(
    order_id: str,
    reason: str,
    current_user_id: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    Permanently delete an order
    
    Restrictions:
    - Cannot delete if prepaid_amount > 0
    - Cannot delete if status is DELIVERED
    - Cannot delete if status is DELIVERY_ALLOTTED
    - Only SUPER_ADMIN and ADMIN can delete
    
    Actions:
    - Releases reserved inventory (for PENDING orders)
    - Deletes order items (cascade)
    - Deletes transactions (cascade)
    - Deletes delivery tracking (cascade)
    - Logs deletion to activity_logs
    """
    try:
        # Fetch the order
        try:
            order = await order_manager.fetch(order_id)
        except Exception as fetch_error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Order not found: {str(fetch_error)}"
            )
        
        # Validation 1: Check prepaid amount
        if order.prepaid_amount > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete order with prepaid amount (Rs {order.prepaid_amount}). Refund required first."
            )
        
        # Validation 2: Check order status
        if order.order_status == OrderStatus.DELIVERED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete DELIVERED orders. This affects sales records and accounting."
            )
        
        if order.order_status == OrderStatus.DELIVERY_ALLOTTED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete DELIVERY_ALLOTTED orders. Order may be in transit."
            )
        
        # Validation 3: Require deletion reason
        if not reason or len(reason.strip()) < 10:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Deletion reason required (minimum 10 characters)"
            )
        
        # Get order items before deletion (for inventory release)
        order_items = await order_item_manager.fetch_all(
            filters={"order_id": order_id}
        )
        
        # Release reserved inventory (only for PENDING and CANCELLED orders)
        if order.order_status in [OrderStatus.PENDING, OrderStatus.CANCELLED]:
            for item in order_items.items:
                try:
                    # Find inventory record at assigned outlet
                    inventory_items = await inventory_manager.fetch_all(
                        filters={
                            "product_id": item.product_id,
                            "outlet_id": order.assigned_outlet_id
                        }
                    )
                    
                    # If not found at outlet, try warehouse
                    if not inventory_items.items:
                        inventory_items = await inventory_manager.fetch_all(
                            filters={
                                "product_id": item.product_id,
                                "outlet_id": None
                            }
                        )
                    
                    if inventory_items.items:
                        inventory_item = inventory_items.items[0]
                        
                        # Only release if order is PENDING (CANCELLED already released)
                        if order.order_status == OrderStatus.PENDING:
                            new_reserved = inventory_item.reserved_quantity - item.quantity
                            
                            await inventory_manager.update(
                                inventory_item.uid,
                                {
                                    "reserved_quantity": max(0, new_reserved),
                                    "last_updated": datetime.utcnow()
                                }
                            )
                except Exception as inv_error:
                    # Log but don't fail deletion if inventory update fails
                    print(f"Warning: Failed to release inventory for item {item.product_id}: {inv_error}")
        
        # Log deletion to activity_logs (if you have ActivityLogManager)
        try:
            from managers import ActivityLogManager
            activity_manager = ActivityLogManager(engine)
            
            # Prepare order data for logging
            order_data = {
                "order_number": order.order_number,
                "customer_name": order.customer_name,
                "customer_phone": order.customer_phone,
                "order_status": order.order_status.value,
                "total_amount": float(order.total_amount),
                "prepaid_amount": float(order.prepaid_amount),
                "items_count": len(order_items.items),
                "deletion_reason": reason
            }
            
            from managers import ActivityLogSchema
            activity_log = ActivityLogSchema(
                user_id=current_user_id,
                action="ORDER_DELETED",
                entity_type="customer_orders",
                entity_id=order_id,
                details=order_data,
                ip_address=None  # Can be added if available from request
            )
            
            await activity_manager.create(activity_log)
        except Exception as log_error:
            # Log but don't fail deletion if activity logging fails
            print(f"Warning: Failed to log deletion activity: {log_error}")
        
        # Delete the order (cascade will handle related tables)
        # Note: Using direct session delete to avoid SharedBackend bug
        try:
            async with order_manager.session_factory() as session:
                # Fetch the order again in this session
                import sqlalchemy as db
                query = db.select(CustomerOrderSchema).filter_by(uid=order_id)
                result = await session.execute(query)
                order_to_delete = result.scalar_one_or_none()
                
                if not order_to_delete:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Order not found during deletion"
                    )
                
                # Delete the order
                await session.delete(order_to_delete)
                await session.commit()
        except HTTPException:
            raise
        except Exception as delete_error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete order from database: {str(delete_error)}"
            )
        
        return StatusResponse(
            status="ok",
            message=f"Order {order.order_number} deleted successfully. Reason: {reason}"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete order: {str(e)}"
        )


@router.post("/{order_id}/revoke", response_model=StatusResponse)
async def revoke_order(
    order_id: str,
    payload: OrderRevokeRequest,
    current_user_id: str = Depends(require_roles(UserRole.SUPER_ADMIN))
):
    """
    Revoke a DELIVERED order back to PENDING status
    
    Use case: Order was marked as DELIVERED by mistake
    
    Restrictions:
    - Only SUPER_ADMIN can revoke orders
    - Can only revoke orders with status DELIVERED
    - Requires mandatory reason (minimum 10 characters)
    
    Actions:
    - Changes status from DELIVERED to PENDING
    - Restores inventory (adds quantity back + reserves it)
    - Clears actual_delivery_date
    - Logs revocation to activity_logs
    """
    try:
        # Fetch the order
        try:
            order = await order_manager.fetch(order_id)
        except Exception as fetch_error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Order not found: {str(fetch_error)}"
            )
        
        # Validation 1: Check order status
        if order.order_status != OrderStatus.DELIVERED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Can only revoke DELIVERED orders. Current status: {order.order_status.value}"
            )
        
        # Validation 2: Reason is already validated by Pydantic model
        # But we'll double-check for safety
        if not payload.reason or len(payload.reason.strip()) < 10:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Revocation reason required (minimum 10 characters)"
            )
        
        # Get order items for inventory restoration
        order_items = await order_item_manager.fetch_all(
            filters={"order_id": order_id}
        )
        
        # Restore inventory: add quantity back + reserve it
        for item in order_items.items:
            try:
                # Find inventory record at assigned outlet
                inventory_items = await inventory_manager.fetch_all(
                    filters={
                        "product_id": item.product_id,
                        "outlet_id": order.assigned_outlet_id
                    }
                )
                
                # If not found at outlet, try warehouse
                if not inventory_items.items:
                    inventory_items = await inventory_manager.fetch_all(
                        filters={
                            "product_id": item.product_id,
                            "outlet_id": None
                        }
                    )
                
                if inventory_items.items:
                    inventory_item = inventory_items.items[0]
                    
                    # Restore: add quantity back + reserve it
                    new_quantity = inventory_item.quantity + item.quantity
                    new_reserved = inventory_item.reserved_quantity + item.quantity
                    
                    await inventory_manager.update(
                        inventory_item.uid,
                        {
                            "quantity": new_quantity,
                            "reserved_quantity": new_reserved,
                            "last_updated": datetime.utcnow()
                        }
                    )
                else:
                    # If inventory record doesn't exist, we can't restore
                    # This shouldn't happen in normal flow, but log it
                    print(f"Warning: No inventory record found for product {item.product_id} at outlet {order.assigned_outlet_id}")
                    
            except Exception as inv_error:
                # If inventory restoration fails, rollback and fail the revoke
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to restore inventory for product {item.product_id}: {str(inv_error)}"
                )
        
        # Update order status to PENDING and clear delivery date
        await order_manager.update(
            order_id,
            {
                "order_status": OrderStatus.PENDING,
                "actual_delivery_date": None,
                "status_remarks": f"REVOKED: {payload.reason}",
                "updated_at": datetime.utcnow()
            }
        )
        
        # Log revocation to activity_logs
        try:
            from managers import ActivityLogManager, ActivityLogSchema
            activity_manager = ActivityLogManager(engine)
            
            # Prepare order data for logging
            order_data = {
                "order_number": order.order_number,
                "customer_name": order.customer_name,
                "customer_phone": order.customer_phone,
                "previous_status": "DELIVERED",
                "new_status": "PENDING",
                "total_amount": float(order.total_amount),
                "items_count": len(order_items.items),
                "revocation_reason": payload.reason,
                "previous_delivery_date": order.actual_delivery_date.isoformat() if order.actual_delivery_date else None
            }
            
            activity_log = ActivityLogSchema(
                user_id=current_user_id,
                action="ORDER_REVOKED",
                entity_type="customer_orders",
                entity_id=order_id,
                details=order_data,
                ip_address=None
            )
            
            await activity_manager.create(activity_log)
        except Exception as log_error:
            # Log but don't fail revocation if activity logging fails
            print(f"Warning: Failed to log revocation activity: {log_error}")
        
        return StatusResponse(
            status="ok",
            message=f"Order {order.order_number} revoked to PENDING. Inventory restored. Reason: {payload.reason}"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to revoke order: {str(e)}"
        )
