from fastapi import APIRouter, HTTPException, Depends, status, Body, Path, Query, BackgroundTasks
from typing import List, Optional, Any, Dict
from datetime import datetime, date, time
from decimal import Decimal
from utils import dependencies as D
from config import get_settings, get_engine
from managers import (
    CustomerOrderManager, OrderItemManager, OrderTransactionManager,
    InventoryManager, ProductManager, OutletManager, UserManager, DeliveryGuyManager,
    OutletMappingManager, DeliveryTrackingManager,InventorySchema,
    CustomerOrderSchema, OrderItemSchema, OrderTransactionSchema, OutletSchema, DeliveryTrackingSchema , ProductSchema
)
from models import (
    OrderCreateRequest, ProxyOrderCreateRequest, OrderUpdateRequest, OrderStatusUpdateRequest,
    OrderAssignRequest, OrderRevokeRequest, OrderTransactionCreateRequest, PaymentStatusUpdateRequest,OrderTransactionUpdateRequest,
    OrderFullUpdateRequest,
    OrderResponse, OrderItemResponse, OrderTransactionResponse,
    ListResponse, StatusResponse, BulkOrderDeliveryAssignmentRequest, BulkAssignmentResponse, BulkAssignmentResult
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, OrderStatus, PaymentStatus, CollectionType, ActivityType, HASSAN_OUTLET_ID
from utils.inventory_utils import is_hassan_or_warehouse, sync_unified_inventory
from services import CRMService , storeService
from utils.outlet_assignment import auto_assign_outlet, push_outlet_not_assigned, push_outlet_assigned
from utils.crm_utils import sync_order_to_crm
import uuid

settings = get_settings()
engine = get_engine(settings.name)

order_manager = CustomerOrderManager(engine)
order_item_manager = OrderItemManager(engine)
transaction_manager = OrderTransactionManager(engine)
inventory_manager = InventoryManager(engine)
product_manager = ProductManager(engine)
outlet_manager = OutletManager(engine)
outlet_mapping_manager = OutletMappingManager(engine)
user_manager = UserManager(engine)
delivery_guy_manager = DeliveryGuyManager(engine)
tracking_manager = DeliveryTrackingManager(engine)

crm_service = CRMService()
store_service = storeService()

router = APIRouter(prefix="/orders", tags=["Order Management"])


def generate_order_number() -> str:
    """Generate unique order number"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"ORD-{timestamp}-{str(uuid.uuid4())[:8].upper()}"



@router.post("/test/test")
async def test(order_id: str):
    result = await sync_order_to_crm(engine, order_id, ActivityType.ORDER_STATUS)
    return result

@router.post("", response_model=OrderResponse)
async def create_order(
    payload: OrderCreateRequest,
    background_tasks: BackgroundTasks,
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
            # Check if product is Silo Fortune (margin = 0) or Non-Silo Fortune (margin > 0)
            if product.margin > 0:
                # Non-Silo Fortune product: Use margin as commission base
                # Ignore product_discount for commission calculation
                commission_base = calculated_unit_price - product.unit_price 
            else:
                # Silo Fortune product: Use existing logic
                # Commission base = cost_price - product_discount
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
            post=payload.post,
            hobli=payload.hobli,
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
            total_commission=total_commission,  # New field
            priority_level=payload.priority_level
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
            assigned_outlet = await auto_assign_outlet(engine, None, payload.district, payload.pincode, payload.state, payload.taluk)
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
        
        background_tasks.add_task(sync_order_to_crm, engine, created_order.uid, ActivityType.CREATE_ORDER)
        background_tasks.add_task(sync_order_to_crm, engine, created_order.uid, ActivityType.ORDER_STATUS)
        background_tasks.add_task(sync_order_to_crm, engine, created_order.uid, ActivityType.DELIVERY_STATUS)

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
                post=getattr(created_order, 'post', None),
                hobli=getattr(created_order, 'hobli', None),
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
                priority_level=created_order.priority_level,
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


@router.post("/proxy", response_model=OrderResponse)
async def create_proxy_order(
    payload: ProxyOrderCreateRequest,
    background_tasks: BackgroundTasks,
    current_user_id: str = Depends(require_roles(UserRole.ADMIN, UserRole.SUPER_ADMIN))
):
    """
    Create order on behalf of a telecaller (Admin/SuperAdmin only)
    
    This endpoint allows admins to create orders that appear as if
    created by the specified telecaller. Useful for:
    - Offline order entry
    - Bulk order imports
    - Order corrections
    - Historical data migration
    
    The order will be attributed to the telecaller_id in payload,
    not the current admin user.
    
    Access: ADMIN, SUPER_ADMIN only
    """
    try:
        # Step 1: Validate telecaller exists and is valid
        try:
            target_telecaller = await user_manager.fetch(payload.telecaller_id)
        except Exception as fetch_error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Telecaller not found: {payload.telecaller_id}"
            )
        
        # Step 2: Verify telecaller is active
        if not target_telecaller.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Telecaller {target_telecaller.full_name} is not active"
            )
        
        # Step 3: Verify user has telecaller-compatible role
        if target_telecaller.role not in [UserRole.TELECALLER, UserRole.ADMIN, UserRole.SUPER_ADMIN]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"User {target_telecaller.full_name} is not a telecaller (role: {target_telecaller.role.value})"
            )
        
        print(f"🔄 PROXY ORDER: Admin {current_user_id} creating order as telecaller {payload.telecaller_id} ({target_telecaller.full_name})")
        
        # Step 4: Validate products and calculate pricing (same as regular order)
        gross_amount = Decimal('0.00')
        product_discount_total = Decimal('0.00')
        total_commission = Decimal('0.00')
        validated_items = []
        
        for item in payload.items:
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
            
            item_gross = item.quantity * product.cost_price
            gross_amount += item_gross
            product_discount_total += item.product_manual_discount
            
            per_unit_discount = item.product_manual_discount / item.quantity if item.quantity > 0 else Decimal('0.00')
            calculated_unit_price = product.cost_price - per_unit_discount
            if calculated_unit_price < 0:
                calculated_unit_price = Decimal('0.00')
            
            subtotal = item.quantity * calculated_unit_price
            
            # Calculate commission for this item
            # Check if product is Silo Fortune (margin = 0) or Non-Silo Fortune (margin > 0)
            if product.margin > 0:
                # Non-Silo Fortune product: Use margin as commission base
                # Ignore product_discount for commission calculation
                commission_base = calculated_unit_price - product.unit_price
            else:
                # Silo Fortune product: Use existing logic
                # Commission base = cost_price - product_discount
                commission_base = max(product.cost_price - per_unit_discount, Decimal('0.00'))
            
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
        
        # Step 5: Calculate final pricing
        discount_applied = product_discount_total
        amount_after_discount = gross_amount - product_discount_total
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
        
        # Step 6: Create order (attributed to telecaller, not admin)
        order_number = generate_order_number()
        
        # IMPORTANT: For proxy orders, NEVER auto-assign to outlet
        # Always use Google API assignment (assigned_outlet_id = None initially)
        assigned_outlet_id = None
        
        new_order = CustomerOrderSchema(
            order_number=order_number,
            customer_name=payload.customer_name,
            customer_phone=payload.customer_phone,
            house_no=payload.house_no,
            street=payload.street,
            address_line=payload.address_line,
            village=payload.village,
            post=payload.post,
            hobli=payload.hobli,
            taluk=payload.taluk,
            district=payload.district,
            state=payload.state,
            pincode=payload.pincode,
            telecaller_id=payload.telecaller_id,  # Use telecaller from payload, not current_user_id
            assigned_outlet_id=assigned_outlet_id,
            order_status=OrderStatus.PENDING,
            collection_type=payload.collection_type,
            payment_method=payload.payment_method,
            order_date=datetime.utcnow(),
            expected_delivery_date=payload.expected_delivery_date,
            gross_amount=gross_amount,
            manual_discount=payload.manual_discount,
            discount_applied=discount_applied,
            prepaid_amount=payload.prepaid_amount,
            total_amount=final_total_amount,
            total_commission=total_commission,
            priority_level=payload.priority_level
        )
        
        created_order = await order_manager.create(new_order)
        
        # Step 7: Create order items
        order_items = []
        for item_data in validated_items:
            try:
                order_item = OrderItemSchema(
                    order_id=created_order.uid,
                    product_id=item_data["product"].uid,
                    quantity=item_data["quantity"],
                    unit_price=item_data["unit_price"],
                    total_price=item_data["subtotal"],
                    subtotal=item_data["subtotal"],
                    product_manual_discount=item_data["product_manual_discount"]
                )
                created_item = await order_item_manager.create(order_item)
                order_items.append(created_item)
            except Exception as item_error:
                print(f"❌ PROXY ORDER ITEM ERROR: {str(item_error)}")
        
        if not order_items and validated_items:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Order created but failed to create order items"
            )
        
        # Step 8: Auto-assign outlet via Google API (always for proxy orders)
        assigned_outlet = await auto_assign_outlet(engine, None, payload.district, payload.pincode, payload.state, payload.taluk)
        if assigned_outlet:
            await order_manager.update(
                created_order.uid,
                {"assigned_outlet_id": assigned_outlet.uid}
            )
            created_order.assigned_outlet_id = assigned_outlet.uid
            assigned_outlet_id = assigned_outlet.uid
        
        # Step 9: Reserve stock
        if assigned_outlet_id:
            try:
                await reserve_order_stock(created_order.uid, assigned_outlet_id, validated_items)
            except Exception as e:
                await order_manager.update(
                    created_order.uid,
                    {
                        "status_remarks": f"Stock reservation failed: {str(e)}",
                        "order_status": OrderStatus.PENDING
                    }
                )
        
        # Step 10: Log proxy order creation for audit trail
        try:
            from managers import ActivityLogManager, ActivityLogSchema
            activity_manager = ActivityLogManager(engine)
            
            activity_log = ActivityLogSchema(
                user_id=current_user_id,  # Admin who created it
                action="PROXY_ORDER_CREATED",
                entity_type="customer_orders",
                entity_id=created_order.uid,
                details={
                    "order_number": created_order.order_number,
                    "telecaller_id": payload.telecaller_id,
                    "telecaller_name": target_telecaller.full_name,
                    "customer_name": payload.customer_name,
                    "customer_phone": payload.customer_phone,
                    "total_amount": float(final_total_amount),
                    "note": "Order created via proxy endpoint by admin"
                },
                ip_address=None
            )
            
            await activity_manager.create(activity_log)
        except Exception as log_error:
            print(f"⚠️  Failed to log proxy order creation: {log_error}")
        
        background_tasks.add_task(sync_order_to_crm, engine, created_order.uid, ActivityType.CREATE_ORDER)

        # Step 11: Build response
        order_items_response = []
        for item in order_items:
            order_items_response.append(OrderItemResponse(
                uid=item.uid,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price=item.unit_price,
                subtotal=item.subtotal,
                product_manual_discount=getattr(item, 'product_manual_discount', Decimal('0.00'))
            ))
        
        return OrderResponse(
            uid=created_order.uid,
            order_number=created_order.order_number,
            customer_name=created_order.customer_name,
            customer_phone=created_order.customer_phone,
            house_no=created_order.house_no,
            street=created_order.street,
            address_line=created_order.address_line,
            village=created_order.village,
            post=getattr(created_order, 'post', None),
            hobli=getattr(created_order, 'hobli', None),
            taluk=created_order.taluk,
            district=created_order.district,
            state=created_order.state,
            pincode=created_order.pincode,
            telecaller_id=created_order.telecaller_id,  # Shows target telecaller
            assigned_outlet_id=assigned_outlet_id,
            order_status=created_order.order_status,
            collection_type=created_order.collection_type,
            payment_method=created_order.payment_method,
            order_date=created_order.order_date,
            expected_delivery_date=created_order.expected_delivery_date,
            actual_delivery_date=created_order.actual_delivery_date,
            status_remarks=created_order.status_remarks,
            gross_amount=gross_amount,
            manual_discount=payload.manual_discount,
            discount_applied=discount_applied,
            prepaid_amount=created_order.prepaid_amount,
            total_amount=final_total_amount,
            total_commission=total_commission,
            priority_level=created_order.priority_level,
            items=order_items_response,
            created_at=created_order.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create proxy order: {str(e)}"
        )




async def _find_inventory_for_product(product_id: str, outlet_id: str):
    """
    Find the best inventory record for a product at a given outlet.
    When the outlet is the Hassan outlet or Warehouse, they are treated
    as a unified pool using the Hassan Outlet ID.
    """
    # If the requested outlet is Warehouse (None) or Hassan Outlet,
    # always use the Hassan Outlet ID for inventory lookup.
    target_outlet_id = outlet_id
    if outlet_id is None or outlet_id == HASSAN_OUTLET_ID:
        target_outlet_id = HASSAN_OUTLET_ID

    inventory_items = await inventory_manager.fetch_all(
        filters={"product_id": product_id, "outlet_id": target_outlet_id}
    )
    
    if inventory_items.items:
        return inventory_items.items[0]

    return None


async def reserve_order_stock(order_id: str, outlet_id: str, validated_items: List[dict]):
    """Reserve stock for order items at assigned outlet"""
    for item_data in validated_items:
        product_id = item_data["product"].uid
        quantity = item_data["quantity"]

        inventory_item = await _find_inventory_for_product(product_id, outlet_id)

        if inventory_item is None:
            raise Exception(f"No stock available for product {item_data['product'].product_name}")

        available = inventory_item.quantity - inventory_item.reserved_quantity
        if available < quantity:
            raise Exception(f"Insufficient stock for {item_data['product'].product_name}. Available: {available}, Required: {quantity}")

        if is_hassan_or_warehouse(outlet_id):
            await sync_unified_inventory(
                inventory_manager,
                InventorySchema,
                product_id=product_id,
                target_outlet_id=outlet_id,
                reserved_delta=quantity
            )
        else:
            await inventory_manager.update(
                inventory_item.uid,
                {
                    "reserved_quantity": inventory_item.reserved_quantity + quantity,
                    "last_updated": datetime.utcnow()
                }
            )

@router.post("/bulk/assign-delivery", response_model=BulkAssignmentResponse)
async def bulk_assign_delivery_guy_to_orders(
    payload: BulkOrderDeliveryAssignmentRequest,
    background_tasks: BackgroundTasks,
    current_user_id: str = Depends(get_current_user_id),
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER, allowed_scopes=["delivery:work"]))
):
    """Bulk assign a delivery guy to multiple orders"""
    try:
        # 1. Validate delivery guy once
        try:
            dg_response = await delivery_guy_manager.fetch_all(filters = {"user_id": payload.delivery_guy_id})
            if not dg_response.items:
                raise HTTPException(status_code=404, detail="Delivery guy profile not found")
            
            dg_profile = dg_response.items[0]
            user = await user_manager.fetch(payload.delivery_guy_id)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=404, detail="Delivery guy not found")
            
        if user.role != UserRole.DELIVERY_GUY:
            raise HTTPException(status_code=400, detail="Assigned user is not a delivery guy")
            
        if not dg_profile.is_active_for_delivery:
            raise HTTPException(status_code=400, detail="Delivery guy is not active for delivery")
            
        results = []
        successful_count = 0
        failed_count = 0
        
        for order_id in payload.order_ids:
            try:
                order = await order_manager.fetch(order_id)
                
                # Check outlet matching
                if dg_profile.outlet_id != order.assigned_outlet_id:
                     results.append(BulkAssignmentResult(
                        order_id=order_id,
                        status="failed",
                        message="Delivery guy outlet does not match order assigned outlet"
                    ))
                     failed_count += 1
                     continue
                
                # Skip if already assigned to this person and already in DELIVERY_ALLOTTED status
                if order.delivery_person_id == user.uid and order.order_status == OrderStatus.DELIVERY_ALLOTTED:
                    results.append(BulkAssignmentResult(order_id=order_id, status="success", message="Already assigned"))
                    successful_count += 1
                    continue
                
                # Update order with delivery person and status
                await order_manager.update(order_id, {
                    "delivery_person_id": user.uid,
                    "order_status": OrderStatus.DELIVERY_ALLOTTED
                })
                
                # --- Simulate External Delivery Service API Call ---
                # import httpx
                # async with httpx.AsyncClient() as client:
                #     await client.post("EXTERNAL_API_URL/assign", json={"order_id": order.uid, "rider_id": user.uid})
                
                results.append(BulkAssignmentResult(order_id=order_id, status="success"))
                successful_count += 1
                
                # Push delivery assignment activity to CRM
                background_tasks.add_task(sync_order_to_crm, engine, order_id, ActivityType.DELIVERY_STATUS)
                
                # 4. Log the status change in tracking table
                tracking_record = DeliveryTrackingSchema(
                    order_id=order_id,
                    outlet_id=dg_profile.outlet_id,
                    telecaller_id=order.telecaller_id,
                    delivery_person_id=user.uid,
                    status_changed_to=OrderStatus.DELIVERY_ALLOTTED,
                    remarks=f"Order assigned to {user.full_name} by outlet manager.",
                    changed_by=current_user_id
                )
                await tracking_manager.create(tracking_record)
                
            except Exception as e:
                results.append(BulkAssignmentResult(
                    order_id=order_id,
                    status="failed",
                    message=str(e)
                ))
                failed_count += 1
                
        return BulkAssignmentResponse(
            successful_count=successful_count,
            failed_count=failed_count,
            results=results
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to bulk assign delivery guy: {str(e)}"
        )

# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    current_user_id: str = Depends(require_roles(
        UserRole.TELECALLER, UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN,
        allowed_scopes=["delivery:read"]
    ))
):
    """Get specific order details"""
    try:
        order = await order_manager.fetch(order_id  ,joins = [CustomerOrderSchema.items])
        
        if current_user_id != "microservice":
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
        return order.model_dump()
    
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


@router.put(
    "/{order_id}/admin", 
    response_model=OrderResponse,
    summary="Update Entire Order Details",
    description="Update all editable fields of an order including customer information, delivery address, order items, discounts, and prepaid amount. Recalculates gross amounts, discounts, and final totals automatically. Requires Admin or Super Admin privileges."
)
async def update_order_full(
    order_id: str = Path(..., description="Unique ID of the order to update"),

    payload: OrderFullUpdateRequest = Body(...,
openapi_examples={
            "full_update": {
                "summary": "Full Order Update Example",
                "description": "Example demonstrating providing new customer details, prepaid amount, and a list of new replacement items.",
                "value": {
                    "customer_name": "test",
                    "customer_phone": "9876543210",
                    "house_no": "A-12",
                    "street": "MG Road",
                    "address_line": "Near Metro Station",
                    "village": "Central",
                    "post": "GPO",
                    "hobli": "South",
                    "taluk": "Bangalore South",
                    "district": "Bangalore Urban",
                    "state": "Karnataka",
                    "pincode": "560001",
                    "collection_type": "DELIVERY",
                    "payment_method": "CASH",
                    "expected_delivery_date": "2023-12-31",
                    "manual_discount": 0.00,
                    "prepaid_amount": 500.00,  # Added the new prepaid_amount field
                    "items": [
                        {
                            "product_id": "prod_12345",
                            "quantity": 2,
                            "product_manual_discount": 10.00
                        }
                    ]
                }
            }
        }
    ),
    current_user_id: str = Depends(require_roles(UserRole.ADMIN, UserRole.SUPER_ADMIN))
):
    try:
        order = await order_manager.fetch(order_id)
        
        if order.order_status in [OrderStatus.DELIVERED, OrderStatus.CANCELLED]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot edit order with status {order.order_status}"
            )

        # 1. Validate products and calculate pricing
        gross_amount = Decimal('0.00')  # Total at MRP
        product_discount_total = Decimal('0.00')
        total_commission = Decimal('0.00')
        validated_items = []
        
        for item in payload.items:
            try:
                product = await product_manager.fetch(item.product_id)
                if not product.is_active:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Product {product.product_name} is not active"
                    )
            except Exception:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product not found: {item.product_id}"
                )
            
            # Calculate grosses and discounts
            item_gross = item.quantity * product.cost_price
            gross_amount += item_gross
            product_discount_total += item.product_manual_discount
            
            # Calculate unit price
            per_unit_discount = item.product_manual_discount / item.quantity if item.quantity > 0 else Decimal('0.00')
            calculated_unit_price = max(Decimal('0.00'), product.cost_price - per_unit_discount)
            subtotal = item.quantity * calculated_unit_price
            
            # Calculate commissions
            if product.margin > 0:
                commission_base = calculated_unit_price - product.unit_price
            else:
                commission_base = max(Decimal('0.00'), product.cost_price - per_unit_discount)
                
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
            
        # 2. Recalculate Final Totals (Using payload.prepaid_amount)
        discount_applied = product_discount_total
        amount_after_discount = gross_amount - product_discount_total
        final_total_amount = amount_after_discount - payload.prepaid_amount
        
        # 3. Validation checks
        if product_discount_total > gross_amount:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Discounts cannot exceed gross amount")
        if payload.prepaid_amount > amount_after_discount:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Prepaid amount exceeds order total after discount")
        if final_total_amount < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Final order amount cannot be negative")
            
        # 4. Update Order Fields mapping
        update_data = {
            "customer_name": payload.customer_name,
            "customer_phone": payload.customer_phone,
            "house_no": payload.house_no,
            "street": payload.street,
            "address_line": payload.address_line,
            "village": payload.village,
            "post": payload.post,
            "hobli": payload.hobli,
            "taluk": payload.taluk,
            "district": payload.district,
            "state": payload.state,
            "pincode": payload.pincode,
            "collection_type": payload.collection_type,
            "payment_method": payload.payment_method,
            "expected_delivery_date": payload.expected_delivery_date,
            "gross_amount": gross_amount,
            "manual_discount": payload.manual_discount,
            "discount_applied": discount_applied,
            "prepaid_amount": payload.prepaid_amount, # <--- Added here to update the row
            "total_amount": final_total_amount,
            "total_commission": total_commission,
            "status_remarks": order.status_remarks  # Carry over in case we need to update it below
        }
    
        # Only release stock if the order status meant the stock was previously reserved (e.g. PENDING)
        if order.order_status == OrderStatus.PENDING and order.assigned_outlet_id:
            await release_order_stock(order_id)

        # 6. Replace items (delete old, create new)
        async with order_manager.session_factory() as session:
            # We must delete manually to ensure consistency
            from sqlalchemy import delete
            await session.execute(delete(OrderItemSchema).where(OrderItemSchema.order_id == order_id))
            await session.commit()
            
        for item_data in validated_items:
            await order_item_manager.create(OrderItemSchema(
                order_id=order_id,
                product_id=item_data["product"].uid,
                quantity=item_data["quantity"],
                unit_price=item_data["unit_price"],
                total_price=item_data["subtotal"],
                subtotal=item_data["subtotal"],
                product_manual_discount=item_data["product_manual_discount"]
            ))

        # 7. Reserve Stock for New Items
        if order.order_status == OrderStatus.PENDING and order.assigned_outlet_id:
            try:
                await reserve_order_stock(order_id, order.assigned_outlet_id, validated_items)
            except Exception as e:
                # If new stock reservation fails, log it in status_remarks (similar to create_order)
                update_data["status_remarks"] = f"Stock reservation failed after update: {str(e)}"
                
        # Commit order level updates to database
        updated_order = await order_manager.update(order_id, update_data)
            
        # 8. Log activity
        try:
            from managers import ActivityLogManager, ActivityLogSchema
            activity_manager = ActivityLogManager(engine)
            await activity_manager.create(ActivityLogSchema(
                user_id=current_user_id,
                action="UPDATE_ORDER_FULL",
                entity_type="customer_order",
                entity_id=order_id,
                details={"updated_fields": list(update_data.keys()), "amount": float(final_total_amount)}
            ))
        except Exception as log_error:
            print(f"Warning: Failed to log activity for order update {order_id}: {log_error}")
            
        return await get_order_response_with_joins(order_id,joins=[
        CustomerOrderSchema.transactions,
        CustomerOrderSchema.telecaller,       # <--- Add this
        CustomerOrderSchema.assigned_outlet,  # <--- Add this
        OrderItemSchema.product               # <--- Add this to prevent the product error!
    ]  )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update order: {str(e)}"
        )


# GENERIC ROUTE LAST (after all specific routes)

@router.get("")
async def get_orders(transfer_status: Optional[OrderStatus] = None,
    telecaller_id: Optional[str] = None,
    outlet_id: Optional[str] = None,
    customer_phone: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    limit: int = 50,
    offset: int = 0,
    dynamic_filters: Dict[str, Any] = Depends(D.filtering_dependency),
    sorts: List[str] = Depends(D.sorting_dependency),
    current_user_id: str = Depends(require_roles(
        UserRole.TELECALLER, UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.DELIVERY_GUY,
        allowed_scopes=["delivery:read"]
    ))
):
    """
    Get orders with filters
    Telecallers see only their orders, managers see outlet orders
    """
    try:
        filters = {}
        # Nested join [items, items.product] ensures products are included for each item
        joins = [
            [CustomerOrderSchema.items, OrderItemSchema.product], 
            CustomerOrderSchema.delivery_person 
        ]
        
        # 1. Role-based isolation (skip for microservice)
        if current_user_id != "microservice":
            current_user = await user_manager.fetch(current_user_id)
            if current_user.role == UserRole.TELECALLER:
                filters["telecaller_id"] = current_user_id
            elif current_user.role == UserRole.OUTLET_MANAGER:
                if current_user.outlet_id:
                    filters["assigned_outlet_id"] = current_user.outlet_id
            elif current_user.role == UserRole.DELIVERY_GUY:
                filters["delivery_person_id"] = current_user_id
        
        # 2. Manual filters
        if transfer_status:
            filters["order_status"] = transfer_status
        
        # Apply optional filters (restricted for non-admins)
        is_admin = current_user_id == "microservice" or current_user.role in [UserRole.ADMIN, UserRole.SUPER_ADMIN]
        
        if telecaller_id and is_admin:
            filters["telecaller_id"] = telecaller_id
        if outlet_id and is_admin:
            filters["assigned_outlet_id"] = outlet_id
        if customer_phone:
            filters["customer_phone"] = customer_phone

        # 3. Date range filters
        if from_date or to_date:
            date_filter = {}
            if from_date:
                date_filter[">="] = datetime.combine(from_date, time.min)
            if to_date:
                date_filter["<="] = datetime.combine(to_date, time.max)
            filters["order_date"] = date_filter

        # 4. Handle type coercion for dynamic filters (expected_delivery_date is a Date column)
        if "expected_delivery_date" in dynamic_filters:
            val = dynamic_filters["expected_delivery_date"]
            if isinstance(val, datetime):
                dynamic_filters["expected_delivery_date"] = val.date()
            elif isinstance(val, dict):
                for op, v in val.items():
                    if isinstance(v, datetime):
                        val[op] = v.date()

        # 5. Merge dynamic filters
        filters.update(dynamic_filters)
        
        orders = await order_manager.fetch_all(
            filters=filters,
            joins=joins,
            limit=limit,
            offset=offset,
            sorts=sorts or ["-created_at"],
        )
        
        return orders.model_dump()
    
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
        UserRole.OUTLET_MANAGER, UserRole.TELECALLER, UserRole.ACCOUNTANT,
        allowed_scopes=["delivery:read"]
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





async def get_order_response(order_id: str) -> OrderResponse:
    """Helper to build complete order response with items"""
    order = await order_manager.fetch(order_id, joins=[CustomerOrderSchema.delivery_person])
    
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
        post=getattr(order, 'post', None),
        hobli=getattr(order, 'hobli', None),
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
        delivery_person_id=order.delivery_person_id,
        delivery_person={
            "full_name": order.delivery_person.full_name,
            "phone": order.delivery_person.phone
        } if order.delivery_person else None,
        items=items,
        created_at=order.created_at
    )

async def get_order_response_with_joins(order_id: str, joins: list) -> OrderResponse:
    """Helper to build complete order response with items and joined relationships"""
    
    from sqlalchemy.orm.attributes import QueryableAttribute
    
    order_joins = []
    item_joins = []
    join_keys = []
    
    for j in (joins or []):
        if isinstance(j, QueryableAttribute):
            join_keys.append(j.key)
            if hasattr(j, "class_") and j.class_.__name__ == "CustomerOrderSchema":
                order_joins.append(j)
            elif hasattr(j, "class_") and j.class_.__name__ == "OrderItemSchema":
                item_joins.append(j)
            else:
                order_joins.append(j)
        else:
            order_joins.append(j)
            if isinstance(j, (list, tuple)) and len(j) > 0 and isinstance(j[0], QueryableAttribute):
                join_keys.append(j[0].key)
    
    # 1. Fetch order with joins (ensure your manager passes 'joins' to the query)
    order = await order_manager.fetch(order_id, joins=order_joins)
    
    # 2. Fetch items (only passing item specific joins)
    order_items = await order_item_manager.fetch_all(
        filters={"order_id": order_id},
        joins=item_joins
    )
    
    from models import OrderItemResponse
    
    # 3. Build Item Responses
    items = []
    for item in order_items.items:
        # Check for joined product data within the item safely without lazy loading
        product_data = item.__dict__.get('product')
        
        items.append(
            OrderItemResponse(
                uid=item.uid,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price=item.unit_price,
                subtotal=item.subtotal,
                product_manual_discount=getattr(item, 'product_manual_discount', Decimal('0.00')),
                # Append product if it was joined
                product=product_data if product_data else None
            )
        )
    
    # 4. Extract Top-Level Joins from the Order Model
    # We map the SQLAlchemy relationship names to the response fields
    telecaller = getattr(order, 'telecaller', None)
    assigned_outlet = getattr(order, 'assigned_outlet', None)
    transactions = getattr(order, 'transactions', []) if "transactions" in join_keys else None
    delivery_tracking = getattr(order, 'delivery_tracking', None) if "delivery_tracking" in join_keys else None

    # Handle backward compatibility for pricing
    gross_amount = getattr(order, 'gross_amount', order.total_amount)
    manual_discount = getattr(order, 'manual_discount', Decimal('0.00'))
    discount_applied = getattr(order, 'discount_applied', Decimal('0.00'))
    prepaid_amount = getattr(order, 'prepaid_amount', Decimal('0.00'))
    total_commission = getattr(order, 'total_commission', Decimal('0.00'))
    
    return OrderResponse(
        uid=order.uid,
        order_number=order.order_number,
        customer_name=order.customer_name,
        customer_phone=order.customer_phone,
        house_no=getattr(order, 'house_no', None),
        street=getattr(order, 'street', None),
        address_line=order.address_line,
        village=getattr(order, 'village', None),
        post=getattr(order, 'post', None),
        hobli=getattr(order, 'hobli', None),
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
        total_commission=total_commission,
        items=items,
        created_at=order.created_at,
        
        # --- Appended Joined Relationships ---
        telecaller=telecaller,
        assigned_outlet=assigned_outlet,
        transactions=transactions,
        delivery_tracking=delivery_tracking
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
        if payload.village is not None:
            update_data["village"] = payload.village
        if payload.post is not None:
            update_data["post"] = payload.post
        if payload.hobli is not None:
            update_data["hobli"] = payload.hobli
        if payload.taluk is not None:
            update_data["taluk"] = payload.taluk
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
    background_tasks: BackgroundTasks,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Update order status (Outlet Manager function)"""
    try:
        order = await order_manager.fetch(order_id, joins=[
            (CustomerOrderSchema.assigned_outlet, OutletSchema.manager),
            (CustomerOrderSchema.items, OrderItemSchema.product)
        ])
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

        update_order = lambda order_obj, update_dict: [setattr(order_obj, k, v) for k, v in update_dict.items()]

        if payload.order_status == OrderStatus.DELIVERED:
            update_data["actual_delivery_date"] = datetime.utcnow()
            
            # Update local object so model_dump() picks it up for CRM
            update_order(order, update_data)
            
            # push the order status to store 
            await store_service.order_delivered(order_id)

            # push activity to crm           
            background_tasks.add_task(sync_order_to_crm, engine, order_id, ActivityType.DELIVERY_STATUS)
            
            # Consume stock from inventory
            await consume_order_stock(order_id)
        
        elif payload.order_status == OrderStatus.CANCELLED:
            if not payload.status_remarks:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Status remarks required for cancelled orders"
                )

            update_order(order, update_data)
            
            # push the order status to store 
            await store_service.order_cancelled(order_id)

            # push activity to crm           
            background_tasks.add_task(sync_order_to_crm, engine, order_id, ActivityType.ORDER_STATUS)
            # Release reserved stock
            await release_order_stock(order_id)

        elif payload.order_status == OrderStatus.DELIVERY_ALLOTTED:
            update_data["actual_delivery_date"] = datetime.utcnow()
            
            # Update local object so model_dump() picks it up for CRM
            update_order(order, update_data)

            # push the order status to store 
            await store_service.order_fulfilled(order_id)

            # push activity to crm           
            background_tasks.add_task(sync_order_to_crm, engine, order_id, ActivityType.DELIVERY_STATUS)
        
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
        OrderStatus.PENDING: [OrderStatus.DELIVERY_ALLOTTED, OrderStatus.CANCELLED, OrderStatus.POSTPONED],
        OrderStatus.DELIVERY_ALLOTTED: [OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.POSTPONED],
        OrderStatus.POSTPONED: [OrderStatus.DELIVERY_ALLOTTED, OrderStatus.CANCELLED],
        OrderStatus.DELIVERED: [],  # Final state
        OrderStatus.CANCELLED: []   # Final state
    }
    
    return new_status in valid_transitions.get(current_status, [])


async def consume_order_stock(order_id: str):
    """Consume reserved stock when order is delivered"""
    order = await order_manager.fetch(order_id)
    order_items = await order_item_manager.fetch_all(filters={"order_id": order_id})

    for item in order_items.items:
        inventory_item = await _find_inventory_for_product(item.product_id, order.assigned_outlet_id)

        if inventory_item:
            if is_hassan_or_warehouse(order.assigned_outlet_id):
                await sync_unified_inventory(
                    inventory_manager,
                    InventorySchema,
                    product_id=item.product_id,
                    target_outlet_id=order.assigned_outlet_id,
                    quantity_delta=-item.quantity,
                    reserved_delta=-item.quantity
                )
            else:
                await inventory_manager.update(
                    inventory_item.uid,
                    {
                        "quantity": max(0, inventory_item.quantity - item.quantity),
                        "reserved_quantity": max(0, inventory_item.reserved_quantity - item.quantity),
                        "last_updated": datetime.utcnow()
                    }
                )


async def release_order_stock(order_id: str):
    """Release reserved stock when order is cancelled"""
    order = await order_manager.fetch(order_id)
    order_items = await order_item_manager.fetch_all(filters={"order_id": order_id})

    for item in order_items.items:
        inventory_item = await _find_inventory_for_product(item.product_id, order.assigned_outlet_id)

        if inventory_item:
            if is_hassan_or_warehouse(order.assigned_outlet_id):
                await sync_unified_inventory(
                    inventory_manager,
                    InventorySchema,
                    product_id=item.product_id,
                    target_outlet_id=order.assigned_outlet_id,
                    reserved_delta=-item.quantity
                )
            else:
                await inventory_manager.update(
                    inventory_item.uid,
                    {
                        "reserved_quantity": max(0, inventory_item.reserved_quantity - item.quantity),
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
        UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN,UserRole.TELECALLER
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
            payment_status=payload.payment_status,
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
        
@router.patch("/{order_id}/transactions/{transaction_uid}", response_model=OrderTransactionResponse)
async def update_order_transaction(
    order_id: str,
    transaction_uid: str,
    payload: OrderTransactionUpdateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Update an existing payment transaction"""
    try:
        # 1. Fetch existing transaction and order
        transaction = await transaction_manager.fetch(transaction_uid)
        if not transaction or transaction.order_id != order_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found for this order"
            )
        # 3. Apply updates
        update_data = payload.dict(exclude_unset=True)
        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No update data provided"
            )

        updated_transaction = await transaction_manager.update(transaction_uid, update_data)
        
        return OrderTransactionResponse.from_orm(updated_transaction)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update transaction: {str(e)}"
        )




@router.put("/{order_id}/payment-status", response_model=StatusResponse)
async def update_order_payment_status(
    order_id: str,
    payload: PaymentStatusUpdateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.TELECALLER
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
        elif current_user.role == UserRole.TELECALLER:
            if order.telecaller_id != current_user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only record payments for your own orders"
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



@router.delete(
    "/{order_id}", 
    response_model=StatusResponse,
    summary="Permanently Delete Order",
    description=(
        "Permanently delete an order.\n\n"
        "Restrictions:\n"
        "- Cannot delete if status is DELIVERED\n"
        "- Cannot delete if status is DELIVERY_ALLOTTED\n"
        "- Only SUPER_ADMIN and ADMIN can delete\n\n"
        "Actions:\n"
        "- Releases reserved inventory (for PENDING orders)\n"
        "- Deletes order items (cascade)\n"
        "- Deletes transactions (cascade)\n"
        "- Deletes delivery tracking (cascade)\n"
        "- Logs deletion to activity_logs"
    )
)
async def delete_order(
    order_id: str = Path(..., description="Unique ID of the order to delete"),
    reason: str = Query(..., min_length=10, description="Reason for deletion (minimum 10 characters)"),
    current_user_id: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
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
                    inventory_item = await _find_inventory_for_product(item.product_id, order.assigned_outlet_id)

                    # Only release if order is PENDING (CANCELLED already released)
                    if inventory_item and order.order_status == OrderStatus.PENDING:
                        if is_hassan_or_warehouse(order.assigned_outlet_id):
                            await sync_unified_inventory(
                                inventory_manager,
                                InventorySchema,
                                product_id=item.product_id,
                                target_outlet_id=order.assigned_outlet_id,
                                reserved_delta=-item.quantity
                            )
                        else:
                            await inventory_manager.update(
                                inventory_item.uid,
                                {
                                    "reserved_quantity": max(0, inventory_item.reserved_quantity - item.quantity),
                                    "last_updated": datetime.utcnow()
                                }
                            )
                except Exception as inv_error:
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
    Revoke a DELIVERED or CANCELLED order back to PENDING status
    
    Use cases:
    - Order was marked as DELIVERED by mistake
    - Order was CANCELLED by mistake and needs to be reactivated
    
    Restrictions:
    - Only SUPER_ADMIN can revoke orders
    - Can only revoke orders with status DELIVERED or CANCELLED
    - Requires mandatory reason (minimum 10 characters)
    
    Actions for DELIVERED orders:
    - Changes status from DELIVERED to PENDING
    - Restores inventory (adds quantity back + reserves it)
    - Clears actual_delivery_date
    - Logs revocation to activity_logs
    
    Actions for CANCELLED orders:
    - Changes status from CANCELLED to PENDING
    - Re-reserves inventory (increases reserved_quantity)
    - Validates stock availability before re-reserving
    - Logs reactivation to activity_logs
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
        if order.order_status not in [OrderStatus.DELIVERED, OrderStatus.CANCELLED]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Can only revoke DELIVERED or CANCELLED orders. Current status: {order.order_status.value}"
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
        
        # Restore inventory based on previous order status
        for item in order_items.items:
            try:
                inventory_item = await _find_inventory_for_product(item.product_id, order.assigned_outlet_id)

                if inventory_item:
                    # Different logic based on previous status
                    if order.order_status == OrderStatus.DELIVERED:
                        # DELIVERED → PENDING: Restore consumed stock + reserve it
                        if is_hassan_or_warehouse(order.assigned_outlet_id):
                            await sync_unified_inventory(
                                inventory_manager,
                                InventorySchema,
                                product_id=item.product_id,
                                target_outlet_id=order.assigned_outlet_id,
                                quantity_delta=item.quantity,
                                reserved_delta=item.quantity
                            )
                        else:
                            await inventory_manager.update(
                                inventory_item.uid,
                                {
                                    "quantity": inventory_item.quantity + item.quantity,
                                    "reserved_quantity": inventory_item.reserved_quantity + item.quantity,
                                    "last_updated": datetime.utcnow()
                                }
                            )

                    elif order.order_status == OrderStatus.CANCELLED:
                        # CANCELLED → PENDING: Re-reserve stock (quantity unchanged)
                        available_stock = inventory_item.quantity - inventory_item.reserved_quantity

                        if available_stock < item.quantity:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Cannot reactivate order: Insufficient stock for product {item.product_id}. "
                                       f"Available: {available_stock}, Required: {item.quantity}. "
                                       f"Stock may have been sold after cancellation."
                            )

                        if is_hassan_or_warehouse(order.assigned_outlet_id):
                            await sync_unified_inventory(
                                inventory_manager,
                                InventorySchema,
                                product_id=item.product_id,
                                target_outlet_id=order.assigned_outlet_id,
                                reserved_delta=item.quantity
                            )
                        else:
                            await inventory_manager.update(
                                inventory_item.uid,
                                {
                                    "reserved_quantity": inventory_item.reserved_quantity + item.quantity,
                                    "last_updated": datetime.utcnow()
                                }
                            )
                else:
                    print(f"Warning: No inventory record found for product {item.product_id} at outlet {order.assigned_outlet_id}")
                    
            except HTTPException:
                # Re-raise HTTP exceptions (like insufficient stock)
                raise
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
                "previous_status": order.order_status.value,  # Dynamic: DELIVERED or CANCELLED
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
        
        # Different message based on previous status
        if order.order_status == OrderStatus.DELIVERED:
            message = f"Order {order.order_number} revoked from DELIVERED to PENDING. Inventory restored. Reason: {payload.reason}"
        elif order.order_status == OrderStatus.CANCELLED:
            message = f"Order {order.order_number} reactivated from CANCELLED to PENDING. Stock re-reserved. Reason: {payload.reason}"
        else:
            message = f"Order {order.order_number} revoked to PENDING. Reason: {payload.reason}"
        
        return StatusResponse(
            status="ok",
            message=message
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to revoke order: {str(e)}"
        )
