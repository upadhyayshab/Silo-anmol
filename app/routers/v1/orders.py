from fastapi import APIRouter, HTTPException, Depends, status, Body, Path, Query, BackgroundTasks
from typing import List, Optional, Any, Dict
from datetime import datetime, date, time, timedelta
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
from utils.auth import require_permission, apply_scope, apply_field_mask, outlet_ids_for_state, AuthContext
from utils.permissions import Permission, ScopeLevel
from utils.constants import UserRole, OrderStatus, PaymentStatus, CollectionType, payment_status_for
from utils.crm_constants import ActivityType
from utils.warehouse_utils import get_default_warehouse_id
from services import CRMService, storeService, deliveryService
from services.deliveryService import ScheduledDeliveryRequest, ScheduledAssignment, ScheduledOrder
from utils.outlet_assignment import auto_assign_outlet, push_outlet_not_assigned, push_outlet_assigned
from utils.crm_utils import sync_order_to_crm
from utils.delivery_utils import build_cumulative_remarks
from utils.smartping_utils import trigger_smartping_event_bg
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
delivery_service = deliveryService()

router = APIRouter(prefix="/orders", tags=["Order Management"])


def generate_order_number(is_crm: bool = False) -> str:
    """Generate unique order number.

    is_crm=True (order placed from a CRM lead — telecaller/proxy) gets the
    ``ORD-CRM-`` prefix so the daily-rev split (CRM vs outlet manager) works;
    outlet-manager/direct orders stay ``ORD-``. Mirrors the legacy LSQ webhook
    prefix (crm.py) so both CRM origins share ``ORD-CRM-``.
    """
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    prefix = "ORD-CRM" if is_crm else "ORD"
    return f"{prefix}-{timestamp}-{str(uuid.uuid4())[:8].upper()}"


def _assert_discount_within_selling_price(product_name: str, per_unit_discount: Decimal, cost_price: Decimal) -> None:
    """A per-unit discount can never exceed the product's selling price (cost_price) — since
    selling price <= MRP (unit_price), this also caps the discount at MRP. Pure/DB-free so it's
    unit-testable like payment_status_for; pinned by test_order_form_validation."""
    if per_unit_discount > cost_price:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Discount for {product_name} exceeds its selling price",
        )


# --- order row-scope helpers (Step 4c) -------------------------------------
# Order visibility is FUNCTION-specific (telecaller=own-created, delivery=assigned-to-
# deliver, managers=by outlet, agency=by agency), so unlike transfers it can't key on a
# single column. These mirror the bespoke per-role filtering the endpoints used before.
_ORDER_OWN_CREATED_ROLES = {"TELECALLER", "AGENCY_TELECALLER"}  # see only orders they created
_ORDER_DELIVERY_ROLES = {"DELIVERY_GUY"}                        # see only orders to deliver


async def _apply_order_scope(filters, ctx):
    """Narrow an orders query to the caller's row scope. GLOBAL/microservice = no narrowing."""
    out = dict(filters or {})
    if ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value:
        return out
    if ctx.role in _ORDER_DELIVERY_ROLES:
        out["delivery_person_id"] = ctx.user_id
        return out
    if ctx.role in _ORDER_OWN_CREATED_ROLES:
        out["telecaller_id"] = ctx.user_id
        return out
    if ctx.scope_level == ScopeLevel.AGENCY.value:
        return await apply_scope(out, ctx)        # telecaller_id IN agency's telecallers
    # geographic managers (OUTLET/CLUSTER/STATE): scope by the order's assigned outlet
    return await apply_scope(out, ctx, outlet_column="assigned_outlet_id")


async def _order_scope_outlet_ids(ctx):
    sv = (await apply_scope({}, ctx, outlet_column="assigned_outlet_id")).get("assigned_outlet_id")
    if sv is None:
        return None
    return sv if isinstance(sv, list) else [sv]


async def _assert_order_in_scope(ctx, order):
    """403 if a scoped caller acts on an order outside their scope. No-op for GLOBAL."""
    if ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value:
        return
    if ctx.role in _ORDER_DELIVERY_ROLES:
        ok = order.delivery_person_id == ctx.user_id
    elif ctx.role in _ORDER_OWN_CREATED_ROLES:
        ok = order.telecaller_id == ctx.user_id
    elif ctx.scope_level == ScopeLevel.AGENCY.value:
        ok = getattr(order, "agency_id", None) in (ctx.agency_ids or [])
    else:
        ids = await _order_scope_outlet_ids(ctx)
        ok = ids is None or order.assigned_outlet_id in ids
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: this order is outside your scope",
        )



@router.post("/test/test")
async def test(order_id: str):
    result = await sync_order_to_crm(engine, order_id, ActivityType.ORDER_STATUS)
    return result

@router.post("", response_model=OrderResponse)
async def create_order(
    payload: OrderCreateRequest,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_WRITE)),
):
    """
    Create new customer order
    - Telecallers: Create orders for phone/online customers
    - Outlet Managers: Create orders for walk-in customers at their outlet
    Automatically reserves stock at assigned outlet
    """
    current_user_id = ctx.user_id
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
            _assert_discount_within_selling_price(product.product_name, per_unit_discount, product.cost_price)
            calculated_unit_price = product.cost_price - per_unit_discount
            # Ensure unit_price is not negative (safety net; the check above already blocks this)
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
        
        # Create order. A lead_id means the order was placed from a CRM lead
        # (telecaller page) -> ORD-CRM- prefix; outlet-manager orders have none -> ORD-.
        order_number = generate_order_number(is_crm=bool(payload.lead_id))

        # Auto-assign to the caller's outlet for outlet-scoped managers (not telecaller/
        # delivery/agency, who create unassigned orders that are auto-routed later).
        assigned_outlet_id = None
        if (ctx.scope_level == ScopeLevel.OUTLET.value
                and ctx.role not in _ORDER_OWN_CREATED_ROLES
                and ctx.role not in _ORDER_DELIVERY_ROLES
                and ctx.outlet_id):
            assigned_outlet_id = ctx.outlet_id
        
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
        
        caller_agency_id = None
        if current_user_id != "microservice":
            _caller = await user_manager.fetch(current_user_id)
            caller_agency_id = getattr(_caller, "agency_id", None)

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
            lead_id=payload.lead_id,
            source=payload.source,
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
            priority_level=payload.priority_level,
            agency_id=caller_agency_id,
        )
        
        # DEBUG: Log the created order schema values
        print(f"🔍 DEBUG: Created order schema:")
        print(f"   • new_order.prepaid_amount = {new_order.prepaid_amount}")
        print(f"   • new_order.manual_discount = {new_order.manual_discount}")
        print(f"   • new_order.total_amount = {new_order.total_amount}")
        
        # Create the order and (when a prepaid payment was provided) its transaction in ONE
        # DB commit. Payment used to be a second HTTP call the frontend made after the order —
        # if it failed, the order was left with no payment record. It now lives here, atomic.
        # ponytail: order+payment share one session; lead-effects/outlet-assignment below stay
        # best-effort as they already were (not part of the order↔payment integrity concern).
        async with order_manager.session_factory() as session:
            created_order = await order_manager.create(new_order, session=session)
            if payload.payment:
                paid = payload.payment.amount_paid
                txn = OrderTransactionSchema(
                    order_id=created_order.uid,
                    # server-computed: fully paid only if it covers the post-discount order value
                    payment_status=payment_status_for(paid, amount_after_discount),
                    payment_method=payload.payment.payment_method,
                    amount_paid=paid,
                    transaction_reference=payload.payment.transaction_reference,
                    payment_date=datetime.utcnow(),
                    received_by=current_user_id,
                    notes=payload.payment.notes,
                )
                await transaction_manager.create(txn, session=session)
            await session.commit()

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

        # CRM: when the order was placed from a lead, drop an entry on that lead's
        # timeline (checkpoint 3.5). The order↔lead link itself lives on
        # customer_orders.lead_id; this is just the human-visible timeline event.
        # Never let a CRM-side failure break order creation.
        if payload.lead_id:
            try:
                from services import leadService
                from utils.crm_enums import LeadActivityType
                await leadService.record_activity(
                    engine, payload.lead_id, LeadActivityType.ORDER,
                    user_id=current_user_id,
                    body=f"Order {order_number} placed — ₹{final_total_amount}",
                    details={
                        "order_id": created_order.uid,
                        "order_number": order_number,
                        "total_amount": str(final_total_amount),
                    },
                )
                
                # Auto-advance FTU/RTU and update counts (checkpoint 3.3)
                await leadService.handle_post_order(engine, payload.lead_id, final_total_amount)

                # Attribution: copy the lead's source/campaign onto the order so reports
                # (which JOIN order_attribution) attribute in-house CRM orders too.
                await leadService.attribute_order(engine, payload.lead_id, created_order.uid)

            except Exception as activity_err:
                print(f"⚠️ Failed to log CRM order activity for lead {payload.lead_id}: {activity_err}")

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
        
        # Stock reservation removed - stock only deducted on delivery
        
        background_tasks.add_task(sync_order_to_crm, engine, created_order.uid, ActivityType.CREATE_ORDER)
        background_tasks.add_task(sync_order_to_crm, engine, created_order.uid, ActivityType.ORDER_STATUS)
        background_tasks.add_task(sync_order_to_crm, engine, created_order.uid, ActivityType.DELIVERY_STATUS)
        background_tasks.add_task(trigger_smartping_event_bg, created_order.uid, "order_confirmation_generic")

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
                agency_id=created_order.agency_id,
                assigned_outlet_id=assigned_outlet_id,
                source=created_order.source,
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

@router.get("/orders-count")
async def get_orders_count(
    filters: Dict[str, Any] = Depends(D.filtering_dependency),
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_READ)),
):
    try:
        filters = await _apply_order_scope(filters, ctx)
        order_count = await order_manager.get_orders_count(filters=filters)
        return {"count": order_count}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch orders count: {str(e)}"
        ) 

@router.get("/orders-count-grouped")
async def get_orders_count_grouped(
    group_by: List[str] = Query(["assigned_outlet_id", "order_status"], description="Columns to group by (comma-separated or multiple params)"),
    filters: Dict[str, Any] = Depends(D.filtering_dependency),
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_READ)),
):
    """
    Get order counts grouped by specified columns.
    If a single column is provided, returns [{"key": val, "count": N}] for backward compatibility.
    If multiple columns are provided, returns [{"col1": val1, "col2": val2, "count": N}].
    """
    try:
        filters = await _apply_order_scope(filters, ctx)
        # Handle comma-separated strings if any (e.g., ?group_by=a,b)
        resolved_groups = []
        for g in group_by:
            if "," in g:
                resolved_groups.extend([x.strip() for x in g.split(",")])
            else:
                resolved_groups.append(g)

        # Pass as string if single column to maintain legacy 'key' format
        pass_to_manager = resolved_groups[0] if len(resolved_groups) == 1 else resolved_groups

        counts = await order_manager.get_orders_count_grouped(
            group_by=pass_to_manager, 
            filters=filters
        )
        return counts
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch grouped orders count: {str(e)}"
        ) 

@router.get("/orders-with-lsq")
async def get_orders_with_lsq(
    filters: Dict[str, Any] = Depends(D.filtering_dependency),
    state: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_READ)),
):
    try:
        filters = await _apply_order_scope(filters, ctx)
        # State filter (super admin): narrow to the outlets in `state`, unless the
        # caller already pinned a specific outlet. Global scope only — scoped roles
        # keep the row scope applied above.
        if state and (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value) \
                and "assigned_outlet_id" not in filters:
            ids = await outlet_ids_for_state(state)
            filters["assigned_outlet_id"] = ids or ["__none__"]
        orders = await order_manager.fetch_all(
            filters=filters,
            joins = [CustomerOrderSchema.lsq_order_ad , (CustomerOrderSchema.items , OrderItemSchema.product)],
            limit=limit,
            offset=offset
        )
        return orders.model_dump()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch orders with lsq: {str(e)}"
        )

@router.post("/proxy", response_model=OrderResponse)
async def create_proxy_order(
    payload: ProxyOrderCreateRequest,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_MANAGE)),
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
        current_user_id = ctx.user_id
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
        
        # Step 6: Create order (attributed to telecaller, not admin). Proxy orders
        # placed from a CRM lead -> ORD-CRM- prefix, same as the telecaller page.
        order_number = generate_order_number(is_crm=bool(payload.lead_id))

        # IMPORTANT: For proxy orders, NEVER auto-assign to outlet
        # Always use Google API assignment (assigned_outlet_id = None initially)
        assigned_outlet_id = None

        _tc = await user_manager.fetch(payload.telecaller_id)
        proxy_agency_id = getattr(_tc, "agency_id", None)

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
            lead_id=payload.lead_id,
            source=payload.source,
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
            priority_level=payload.priority_level,
            agency_id=proxy_agency_id,
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
        
        # Stock reservation removed
        
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
            
        # CRM: proxy orders placed for a lead
        if payload.lead_id:
            try:
                from services import leadService
                from utils.crm_enums import LeadActivityType
                await leadService.record_activity(
                    engine, payload.lead_id, LeadActivityType.ORDER,
                    user_id=current_user_id,
                    body=f"Order {created_order.order_number} placed (Proxy) — ₹{final_total_amount}",
                    details={
                        "order_id": created_order.uid,
                        "order_number": created_order.order_number,
                        "total_amount": str(final_total_amount),
                    },
                )
                
                # Auto-advance FTU/RTU and update counts (checkpoint 3.3)
                await leadService.handle_post_order(engine, payload.lead_id, final_total_amount)

                # Attribution: copy the lead's source/campaign onto the order (see create path).
                await leadService.attribute_order(engine, payload.lead_id, created_order.uid)

            except Exception as activity_err:
                print(f"⚠️ Failed to log CRM proxy order activity for lead {payload.lead_id}: {activity_err}")
        
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
            agency_id=created_order.agency_id,
            assigned_outlet_id=assigned_outlet_id,
            source=created_order.source,
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
    """Find inventory record for a product at a given outlet."""
    target_outlet_id = outlet_id
    if target_outlet_id is None or target_outlet_id == "null":
        target_outlet_id = await get_default_warehouse_id(engine)

    inventory_items = await inventory_manager.fetch_all(
        filters={"product_id": product_id, "outlet_id": target_outlet_id}
    )
    if inventory_items.items:
        return inventory_items.items[0]
    return None


async def reserve_order_stock(order_id: str, outlet_id: str, validated_items: List[dict]):
    """Stock reservation logic removed."""
    return


async def release_order_stock(order_id: str):
    """Stock reservation logic removed."""
    return

@router.post("/bulk/assign-delivery", response_model=BulkAssignmentResponse)
async def bulk_assign_delivery_guy_to_orders(
    payload: BulkOrderDeliveryAssignmentRequest,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_STATUS, allow_scopes=["delivery:work"])),
):
    """Bulk assign a delivery guy to multiple orders"""
    try:
        current_user_id = ctx.user_id
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
        crm_sync_orders = []
        
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

                # Skip ERP update if already assigned to this person and already in DELIVERY_ALLOTTED status
                if order.delivery_person_id == user.uid and order.order_status == OrderStatus.DELIVERY_ALLOTTED:
                    results.append(BulkAssignmentResult(order_id=order_id, status="success", message="Already assigned"))
                    successful_count += 1
                    continue
                
                # Update order with delivery person and status
                await order_manager.update(order_id, {
                    "delivery_person_id": user.uid,
                    "order_status": OrderStatus.DELIVERY_ALLOTTED
                })

                # Internal CRM: record the allotment on the linked lead's timeline.
                from services import leadService
                background_tasks.add_task(
                    leadService.log_order_status_change, engine, order,
                    OrderStatus.DELIVERY_ALLOTTED, current_user_id,
                    old_status=order.order_status,
                    remarks=f"Allotted to {user.full_name} for delivery",
                )

                results.append(BulkAssignmentResult(order_id=order_id, status="success"))
                successful_count += 1
                
                # Defer CRM sync
                crm_sync_orders.append(order_id)
                background_tasks.add_task(trigger_smartping_event_bg, order_id, "order_dispatched")
                
                # 4. Log the status change in tracking table
                existing_tracking = await tracking_manager.fetch_all(
                    filters={"order_id": order_id}, sorts=["created_at"]
                )
                allotment_remark = f"Order assigned to {user.full_name} by outlet manager."
                tracking_record = DeliveryTrackingSchema(
                    order_id=order_id,
                    outlet_id=dg_profile.outlet_id,
                    telecaller_id=order.telecaller_id,
                    delivery_person_id=user.uid,
                    status_changed_to=OrderStatus.DELIVERY_ALLOTTED,
                    remarks=build_cumulative_remarks(
                        existing_tracking.items, OrderStatus.DELIVERY_ALLOTTED, allotment_remark
                    ),
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

        # Fetch all active delivery guys for this outlet
        all_dg_response = await delivery_guy_manager.fetch_all(
            filters={"outlet_id": dg_profile.outlet_id}, limit=100
        )
        
        assignments = []
        for dg in all_dg_response.items:
            if not dg.is_active_for_delivery:
                continue
                
            driver_orders = await order_manager.fetch_all(
                filters={"delivery_person_id": dg.user_id}, limit=1000
            )
            
            s_orders = []
            for order in driver_orders.items:
                s_orders.append(ScheduledOrder(
                    order_id=order.uid,
                    address=f"{order.house_no or ''} {order.street or ''} {order.address_line}".strip(),
                    pincode=order.pincode,
                    latitude=str(order.lat_lon[0]) if order.lat_lon and len(order.lat_lon) > 0 else "0",
                    longitude=str(order.lat_lon[1]) if order.lat_lon and len(order.lat_lon) > 1 else "0",
                    priority=order.priority_level or 10
                ))
                
            if s_orders:
                assignments.append(ScheduledAssignment(
                    driver_uid=dg.user_id,
                    orders=s_orders,
                    total_distance=0.0
                ))

        # Trigger scheduling if there are any assignments
        if assignments:
            print(f"DEBUG: Adding background task for {len(assignments)} drivers to delivery_service")
            scheduling_payload = ScheduledDeliveryRequest(
                outlet_id=dg_profile.outlet_id,
                assignments=assignments
            )
            background_tasks.add_task(delivery_service.create_scheduled_delivery, scheduling_payload)

        # Process CRM syncs after scheduling task to avoid blocking it
        for sync_order_id in crm_sync_orders:
            background_tasks.add_task(sync_order_to_crm, engine, sync_order_id, ActivityType.DELIVERY_STATUS)

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
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_READ, allow_scopes=["delivery:read"])),
):
    """Get specific order details"""
    try:
        order = await order_manager.fetch(order_id, joins = [CustomerOrderSchema.items, CustomerOrderSchema.telecaller])

        await _assert_order_in_scope(ctx, order)
        return apply_field_mask("orders", ctx, order.model_dump())
    
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
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_MANAGE)),
):
    try:
        current_user_id = ctx.user_id
        order = await order_manager.fetch(order_id)

        if order.order_status == OrderStatus.CANCELLED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot edit order with status {order.order_status}"
            )

        # Capture old values for activity log
        old_items_records = await order_item_manager.fetch_all(filters={"order_id": order_id})
        old_values = {
            "customer_name": order.customer_name,
            "customer_phone": order.customer_phone,
            "house_no": order.house_no,
            "street": order.street,
            "address_line": order.address_line,
            "village": order.village,
            "post": order.post,
            "hobli": order.hobli,
            "taluk": order.taluk,
            "district": order.district,
            "state": order.state,
            "pincode": order.pincode,
            "collection_type": order.collection_type.value if hasattr(order.collection_type, "value") else order.collection_type,
            "payment_method": order.payment_method.value if hasattr(order.payment_method, "value") else order.payment_method,
            "expected_delivery_date": order.expected_delivery_date.isoformat() if order.expected_delivery_date else None,
            "manual_discount": float(order.manual_discount),
            "prepaid_amount": float(order.prepaid_amount),
            "total_amount": float(order.total_amount),
            "priority_level": order.priority_level,
            "items": [
                {
                    "product_id": item.product_id,
                    "quantity": item.quantity,
                    "unit_price": float(item.unit_price),
                    "product_manual_discount": float(item.product_manual_discount)
                }
                for item in old_items_records.items
            ]
        }

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
            "priority_level": payload.priority_level,
            "status_remarks": order.status_remarks  # Carry over in case we need to update it below
        }
    
        # Revert old stock if the order status is DELIVERED
        is_delivered = order.order_status == OrderStatus.DELIVERED
        if is_delivered and order.assigned_outlet_id:
            for item in old_items_records.items:
                inv = await _find_inventory_for_product(item.product_id, order.assigned_outlet_id)
                if inv:
                    await inventory_manager.update(inv.uid, {
                        "quantity": inv.quantity + item.quantity,
                        "last_updated": datetime.utcnow()
                    })

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

        # Deduct new stock if the order status is DELIVERED
        if is_delivered and order.assigned_outlet_id:
            for item_data in validated_items:
                inv = await _find_inventory_for_product(item_data["product"].uid, order.assigned_outlet_id)
                if inv:
                    await inventory_manager.update(inv.uid, {
                        "quantity": max(0, inv.quantity - item_data["quantity"]),
                        "last_updated": datetime.utcnow()
                    })
                
        # Commit order level updates to database
        updated_order = await order_manager.update(order_id, update_data)
            
        # 8. Log activity
        new_values = {
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
            "collection_type": payload.collection_type.value if hasattr(payload.collection_type, "value") else payload.collection_type,
            "payment_method": payload.payment_method.value if hasattr(payload.payment_method, "value") else payload.payment_method,
            "expected_delivery_date": payload.expected_delivery_date.isoformat() if payload.expected_delivery_date else None,
            "manual_discount": float(payload.manual_discount),
            "prepaid_amount": float(payload.prepaid_amount),
            "total_amount": float(final_total_amount),
            "priority_level": payload.priority_level,
            "items": [
                {
                    "product_id": item_data["product"].uid,
                    "quantity": item_data["quantity"],
                    "unit_price": float(item_data["unit_price"]),
                    "product_manual_discount": float(item_data["product_manual_discount"])
                }
                for item_data in validated_items
            ]
        }

        try:
            from managers import ActivityLogManager, ActivityLogSchema
            activity_manager = ActivityLogManager(engine)
            await activity_manager.create(ActivityLogSchema(
                user_id=current_user_id,
                action="UPDATE_ORDER_FULL",
                entity_type="customer_order",
                entity_id=order_id,
                details={
                    "old_values": old_values,
                    "new_values": new_values,
                    "updated_fields": list(update_data.keys()),
                    "amount": float(final_total_amount)
                }
            ))
        except Exception as log_error:
            print(f"Warning: Failed to log activity for order update {order_id}: {log_error}")
            
        return await get_order_response_with_joins(order_id,joins=[
        CustomerOrderSchema.transactions,
        CustomerOrderSchema.telecaller,
        CustomerOrderSchema.assigned_outlet,
        CustomerOrderSchema.delivery_person,   # <--- Add this
        OrderItemSchema.product
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
    state: Optional[str] = None,
    customer_phone: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    limit: int = 50,
    offset: int = 0,
    dynamic_filters: Dict[str, Any] = Depends(D.filtering_dependency),
    sorts: List[str] = Depends(D.sorting_dependency),
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_READ, allow_scopes=["delivery:read"])),
):
    """
    Get orders with filters
    Telecallers see only their orders, managers see outlet orders
    """
    try:
        # Nested join [items, items.product] ensures products are included for each item
        joins = [
            [CustomerOrderSchema.items, OrderItemSchema.product],
            CustomerOrderSchema.delivery_person,
            CustomerOrderSchema.telecaller,
            CustomerOrderSchema.assigned_outlet
        ]

        # 1. Row scope (telecaller=own / delivery=assigned / managers=outlet / agency / global=all)
        filters = await _apply_order_scope({}, ctx)

        # 2. Manual filters
        if transfer_status:
            filters["order_status"] = transfer_status

        # Cross-cutting filters by arbitrary telecaller/outlet only for unrestricted callers.
        is_admin = ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value

        if telecaller_id and is_admin:
            filters["telecaller_id"] = telecaller_id
        if outlet_id and is_admin:
            filters["assigned_outlet_id"] = outlet_id
        # State filter (super admin): narrow to the outlets in `state`. A specific
        # outlet_id is more specific and wins if both are supplied.
        elif state and is_admin:
            ids = await outlet_ids_for_state(state)
            filters["assigned_outlet_id"] = ids or ["__none__"]
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

        # 4. Handle type coercion for dynamic filters (Date-only columns or columns where date-level filtering is common)
        date_columns = ["expected_delivery_date", "created_at", "updated_at", "order_date", "actual_delivery_date"]
        for date_col in date_columns:
            if date_col in dynamic_filters:
                val = dynamic_filters[date_col]
                if isinstance(val, datetime):
                    dynamic_filters[date_col] = val.date()
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

        result = orders.model_dump()
        result["items"] = apply_field_mask("orders", ctx, result.get("items", []))
        return result
    
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
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_READ, allow_scopes=["delivery:read"])),
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
        
        # Scope: telecaller -> own orders, managers -> their outlet(s); global -> all.
        phone_filters = await _apply_order_scope({"customer_phone": sanitized_phone}, ctx)
        orders = await order_manager.fetch_all(
            filters=phone_filters,
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

        order_responses = apply_field_mask("orders", ctx, order_responses)
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
    items = []
    for item in order_items.items:
        inv = await _find_inventory_for_product(item.product_id, order.assigned_outlet_id)
        items.append(
            OrderItemResponse(
                uid=item.uid,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price=item.unit_price,
                subtotal=item.subtotal,
                product_manual_discount=getattr(item, 'product_manual_discount', Decimal('0.00')),
                inventory_quantity=inv.quantity if inv else 0
            )
        )
    
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
        agency_id=order.agency_id,
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
        inv = await _find_inventory_for_product(item.product_id, order.assigned_outlet_id)
        
        items.append(
            OrderItemResponse(
                uid=item.uid,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price=item.unit_price,
                subtotal=item.subtotal,
                product_manual_discount=getattr(item, 'product_manual_discount', Decimal('0.00')),
                # Append product if it was joined
                product=product_data if product_data else None,
                inventory_quantity=inv.quantity if inv else 0
            )
        )
    
    # 4. Extract Top-Level Joins from the Order Model
    # We map the SQLAlchemy relationship names to the response fields
    # Use __dict__.get to avoid lazy loading on detached instances
    telecaller = order.__dict__.get('telecaller')
    assigned_outlet = order.__dict__.get('assigned_outlet')
    transactions = order.__dict__.get('transactions', []) if "transactions" in join_keys else None
    delivery_tracking = order.__dict__.get('delivery_tracking') if "delivery_tracking" in join_keys else None

    # Handle backward compatibility for pricing
    gross_amount = getattr(order, 'gross_amount', order.total_amount)
    manual_discount = getattr(order, 'manual_discount', Decimal('0.00'))
    discount_applied = getattr(order, 'discount_applied', Decimal('0.00'))
    prepaid_amount = getattr(order, 'prepaid_amount', Decimal('0.00'))
    total_commission = getattr(order, 'total_commission', Decimal('0.00'))
    priority_level = getattr(order, 'priority_level', 10)
    
    delivery_person_obj = order.__dict__.get('delivery_person')
    delivery_person_dict = {
        "full_name": delivery_person_obj.full_name,
        "phone": delivery_person_obj.phone
    } if delivery_person_obj else None

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
        agency_id=order.agency_id,
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
        priority_level=priority_level,
        lat_lon=getattr(order, 'lat_lon', None),
        delivery_person_id=getattr(order, 'delivery_person_id', None),
        delivery_person=delivery_person_dict,
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
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_WRITE)),
):
    """Update order details (only for pending orders)"""
    try:
        order = await order_manager.fetch(order_id)

        await _assert_order_in_scope(ctx, order)
        
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


def _transition_allowed(current: OrderStatus, new: OrderStatus, *, allow_uncancel: bool = False) -> bool:
    """Transition gate for status changes.

    allow_uncancel is the bulk super-admin extra: a CANCELLED order may be
    revived to PENDING. DELIVERED stays final everywhere.
    """
    if allow_uncancel and current == OrderStatus.CANCELLED and new == OrderStatus.PENDING:
        return True
    return is_valid_status_transition(current, new)


@router.put("/{order_id}/status", response_model=StatusResponse)
async def update_order_status(
    order_id: str,
    payload: OrderStatusUpdateRequest,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_STATUS)),
):
    """Update order status (Outlet Manager function)"""
    try:
        order = await order_manager.fetch(order_id, joins=[
            (CustomerOrderSchema.assigned_outlet, OutletSchema.manager),
            (CustomerOrderSchema.items, OrderItemSchema.product)
        ])
        await _assert_order_in_scope(ctx, order)
        current_user_id = ctx.user_id

        old_status = order.order_status

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
            background_tasks.add_task(trigger_smartping_event_bg, order_id, "order_delivered")
            
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
            # Stock reservation logic removed

        elif payload.order_status == OrderStatus.DELIVERY_ALLOTTED:
            update_data["actual_delivery_date"] = datetime.utcnow()
            
            # Update local object so model_dump() picks it up for CRM
            update_order(order, update_data)

            # push the order status to store 
            await store_service.order_fulfilled(order_id)

            # push activity to crm           
            background_tasks.add_task(sync_order_to_crm, engine, order_id, ActivityType.DELIVERY_STATUS)
            background_tasks.add_task(trigger_smartping_event_bg, order_id, "order_dispatched")

        elif payload.order_status == OrderStatus.PENDING:
            if not payload.status_remarks:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Status remarks required when reverting to pending"
                )
            
            update_data["delivery_person_id"] = None
            update_data["actual_delivery_date"] = None
            
            update_order(order, update_data)
            
            # Create a tracking record for the unassignment
            existing_tracking = await tracking_manager.fetch_all(
                filters={"order_id": order_id}, sorts=["created_at"]
            )
            unassign_remark = f"Order unassigned. Reason: {payload.status_remarks}"
            tracking_record = DeliveryTrackingSchema(
                order_id=order_id,
                outlet_id=order.assigned_outlet_id,
                telecaller_id=order.telecaller_id,
                delivery_person_id=None,
                status_changed_to=OrderStatus.PENDING,
                remarks=build_cumulative_remarks(
                    existing_tracking.items, OrderStatus.PENDING, unassign_remark
                ),
                changed_by=current_user_id
            )
            await tracking_manager.create(tracking_record)

            # Notify CRM that status is back to Pending
            # background_tasks.add_task(sync_order_to_crm, engine, order_id, ActivityType.ORDER_STATUS)


        elif payload.order_status in [
            OrderStatus.POSTPONED,
            OrderStatus.ATTEMPTED,
            OrderStatus.CUSTOMER_NOT_AVAILABLE,
            OrderStatus.UNABLE_TO_CONTACT,
            OrderStatus.UNABLE_TO_LOCATE,
            OrderStatus.PAYMENT_NOT_READY
        ]:
            if not payload.status_remarks:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Status remarks required for {payload.order_status.value}"
                )
            
            update_data["priority_level"] = (order.priority_level or 0) + 10
            
            if payload.order_status in [OrderStatus.POSTPONED, OrderStatus.PAYMENT_NOT_READY] and payload.postpone_date:
                update_data["expected_delivery_date"] = payload.postpone_date
            else:
                update_data["expected_delivery_date"] = datetime.utcnow().date() + timedelta(days=1)
            
            update_order(order, update_data)
            
            # push activity to crm           
            background_tasks.add_task(sync_order_to_crm, engine, order_id, ActivityType.DELIVERY_STATUS)
        
        await order_manager.update(order_id, update_data)

        # Internal CRM: log the status change on the lead timeline, attributed
        # to the user who made the change.
        from services import leadService
        background_tasks.add_task(
            leadService.log_order_status_change, engine, order,
            payload.order_status, current_user_id,
            old_status=old_status, remarks=payload.status_remarks,
        )

        return StatusResponse(
            status="ok",
            message=f"Order status updated to {payload.order_status.value}"
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
    
    logistics_statuses = [
        OrderStatus.ATTEMPTED,
        OrderStatus.CUSTOMER_NOT_AVAILABLE,
        OrderStatus.UNABLE_TO_CONTACT,
        OrderStatus.UNABLE_TO_LOCATE,
        OrderStatus.PAYMENT_NOT_READY
    ]
    
    valid_transitions = {
        OrderStatus.PENDING: [OrderStatus.DELIVERY_ALLOTTED, OrderStatus.CANCELLED, OrderStatus.POSTPONED] + logistics_statuses,
        OrderStatus.DELIVERY_ALLOTTED: [OrderStatus.PENDING, OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.POSTPONED] + logistics_statuses,
        OrderStatus.POSTPONED: [OrderStatus.DELIVERY_ALLOTTED, OrderStatus.CANCELLED],
        OrderStatus.DELIVERED: [],  # Final state
        OrderStatus.CANCELLED: []   # Final state
    }
    
    for status in logistics_statuses:
        valid_transitions[status] = [
            OrderStatus.DELIVERY_ALLOTTED,
            OrderStatus.DELIVERED,
            OrderStatus.CANCELLED,
            OrderStatus.POSTPONED
        ]
    
    return new_status in valid_transitions.get(current_status, [])


async def consume_order_stock(order_id: str):
    """Decrease quantity when order is delivered"""
    order = await order_manager.fetch(order_id)
    order_items = await order_item_manager.fetch_all(filters={"order_id": order_id})

    for item in order_items.items:
        inventory_item = await _find_inventory_for_product(item.product_id, order.assigned_outlet_id)

        if inventory_item:
            # Simply decrease quantity (no reserved_quantity logic)
            await inventory_manager.update(
                inventory_item.uid,
                {
                    "quantity": max(0, inventory_item.quantity - item.quantity),
                    "last_updated": datetime.utcnow()
                }
            )


# release_order_stock removed (handled by dummy function above)


@router.put("/{order_id}/assign", response_model=StatusResponse)
async def assign_order_to_outlet(
    order_id: str,
    payload: OrderAssignRequest,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_MANAGE)),
):
    """Manually assign or re-assign an order to an outlet (Admin function).

    Works for any non-terminal order. Delivered and cancelled orders cannot be
    re-assigned. If the order had already moved past PENDING (e.g. allotted to a
    delivery person, postponed, or returned from logistics), it is reset to
    PENDING and its delivery person is cleared — that rider belongs to the
    previous outlet, so the new outlet must re-allot delivery.
    """
    # Orders in a terminal state can never be re-assigned.
    NON_REASSIGNABLE = {OrderStatus.DELIVERED, OrderStatus.CANCELLED}
    current_user_id = ctx.user_id
    try:
        order = await order_manager.fetch(order_id)

        if order.order_status in NON_REASSIGNABLE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot re-assign a {order.order_status.value} order"
            )

        # No-op guard: already at the requested outlet
        if order.assigned_outlet_id == payload.assigned_outlet_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Order is already assigned to this outlet"
            )

        # Verify outlet exists
        try:
            outlet = await outlet_manager.fetch(payload.assigned_outlet_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Outlet not found"
            )
        if not outlet.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot assign to inactive outlet"
            )

        # If the order was past PENDING, reset it: the assigned delivery person
        # belongs to the old outlet and any logistics state no longer applies.
        previous_status = order.order_status
        previous_delivery_person_id = order.delivery_person_id
        was_in_flight = previous_status != OrderStatus.PENDING

        update_data = {"assigned_outlet_id": payload.assigned_outlet_id}
        if was_in_flight:
            update_data["order_status"] = OrderStatus.PENDING
            update_data["delivery_person_id"] = None
            update_data["actual_delivery_date"] = None

        # Update assignment (no stock reservation/release — reservation removed)
        await order_manager.update(order_id, update_data)

        # Internal CRM: record the reset-to-pending on the linked lead's timeline.
        if was_in_flight:
            try:
                from services import leadService
                await leadService.log_order_status_change(
                    engine, order, OrderStatus.PENDING, current_user_id,
                    old_status=previous_status,
                    remarks=f"Re-assigned to outlet {outlet.outlet_name}; reset to pending",
                )
            except Exception as activity_err:
                print(f"⚠️ Failed to log CRM reassign activity for lead {order.lead_id}: {activity_err}")

        # Record the re-assignment in delivery tracking so history is auditable.
        # Best-effort only: the order has already been re-assigned and committed
        # above, so an audit-log write must never fail the operation. The
        # delivery_tracking table (in prod) requires non-null telecaller_id AND
        # delivery_person_id, so the row records the rider the order was taken
        # away from, and is skipped when either is missing (e.g. an order that
        # was never allotted) rather than failing the request.
        if was_in_flight and order.telecaller_id and previous_delivery_person_id:
            try:
                existing_tracking = await tracking_manager.fetch_all(
                    filters={"order_id": order_id}, sorts=["created_at"]
                )
                reassign_remark = (
                    f"Order re-assigned to outlet {outlet.outlet_name} "
                    f"(was {previous_status.value}); reset to pending."
                )
                tracking_record = DeliveryTrackingSchema(
                    order_id=order_id,
                    outlet_id=payload.assigned_outlet_id,
                    telecaller_id=order.telecaller_id,
                    delivery_person_id=previous_delivery_person_id,
                    status_changed_to=OrderStatus.PENDING,
                    remarks=build_cumulative_remarks(
                        existing_tracking.items, OrderStatus.PENDING, reassign_remark
                    ),
                    changed_by=current_user_id
                )
                await tracking_manager.create(tracking_record)
            except Exception as track_err:
                print(
                    f"WARNING: re-assignment tracking record not written for "
                    f"{order_id}: {type(track_err).__name__}: {track_err}"
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
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_WRITE)),
):
    """Add payment transaction to order"""
    try:
        current_user_id = ctx.user_id
        order = await order_manager.fetch(order_id)

        await _assert_order_in_scope(ctx, order)
        
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
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_MANAGE)),
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
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_WRITE)),
):
    """
    Update payment status of an order
    Used to mark orders as paid, partially paid, etc.
    """
    try:
        current_user_id = ctx.user_id
        # Verify order exists and check access
        order = await order_manager.fetch(order_id)

        await _assert_order_in_scope(ctx, order)
        
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
            # Internal CRM: record the delivered transition on the lead timeline.
            if order.lead_id:
                try:
                    from services import leadService
                    await leadService.log_order_status_change(
                        engine, order, OrderStatus.DELIVERED, current_user_id,
                        old_status=order.order_status,
                        remarks="Marked delivered (payment received)",
                    )
                except Exception as activity_err:
                    print(f"⚠️ Failed to log CRM delivered activity for lead {order.lead_id}: {activity_err}")
        
        # CRM Logging
        if order.lead_id:
            try:
                from services import leadService
                from utils.crm_enums import LeadActivityType
                await leadService.record_activity(
                    engine, order.lead_id, LeadActivityType.ORDER_UPDATE,
                    user_id=current_user_id,
                    body=f"Order {order.order_number} payment status updated to {payload.payment_status.value}",
                    details={
                        "order_id": order.uid,
                        "order_number": order.order_number,
                        "payment_status": payload.payment_status.value,
                        "notes": payload.notes
                    },
                )
            except Exception as activity_err:
                print(f"⚠️ Failed to log CRM order payment activity for lead {order.lead_id}: {activity_err}")
                
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
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_MANAGE)),
):
    try:
        current_user_id = ctx.user_id
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
        
        # Inventory reservation logic removed.
        
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
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_REVOKE)),
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
        current_user_id = ctx.user_id
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
                    # Stock restored only if revoking from DELIVERED
                    if order.order_status == OrderStatus.DELIVERED:
                        await inventory_manager.update(
                            inventory_item.uid,
                            {
                                "quantity": inventory_item.quantity + item.quantity,
                                "last_updated": datetime.utcnow()
                            }
                        )
                    # For CANCELLED orders, no stock was reserved/consumed, so no change
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

        # Internal CRM: also record the revocation on the linked lead's timeline.
        if order.lead_id:
            try:
                from services import leadService
                await leadService.log_order_status_change(
                    engine, order, OrderStatus.PENDING, current_user_id,
                    old_status=order.order_status,  # still DELIVERED/CANCELLED in-memory
                    remarks=f"Revoked: {payload.reason}",
                )
            except Exception as activity_err:
                print(f"⚠️ Failed to log CRM revoke activity for lead {order.lead_id}: {activity_err}")

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
