from fastapi import APIRouter, HTTPException, Depends, status, Body, Path, Query, BackgroundTasks
from fastapi.responses import StreamingResponse
from typing import List, Optional, Any, Dict
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal
import logging
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
    ListResponse, StatusResponse, BulkOrderDeliveryAssignmentRequest, BulkAssignmentResponse, BulkAssignmentResult,
    BulkOrderStatusUpdateRequest, BulkOrderStatusUpdateResult, BulkOrderStatusUpdateResponse,
    BulkOrderReassignItem, BulkOrderReassignRequest, BulkOrderReassignResult, BulkOrderReassignResponse
)
from utils.auth import require_permission, apply_scope, apply_field_mask, outlet_ids_for_state, AuthContext
from utils.permissions import Permission, ScopeLevel
from utils.constants import UserRole, OrderStatus, PaymentStatus, CollectionType, payment_status_for, OrderEventType, EscalationState
from utils.crm_constants import ActivityType
from utils.warehouse_utils import get_default_warehouse_id
from services import CRMService, storeService, deliveryService, order_events_service
from services.orderExportService import stream_orders_csv
from services.order_events_service import fold_order_state
from services.deliveryService import ScheduledDeliveryRequest, ScheduledAssignment, ScheduledOrder
from utils.outlet_assignment import auto_assign_outlet, push_outlet_not_assigned, push_outlet_assigned
from utils.crm_utils import sync_order_to_crm
from utils.delivery_utils import build_cumulative_remarks
from utils.smartping_utils import trigger_smartping_event_bg
from utils.timeutils import ist_range_bounds, ist_today
import uuid


def _canon_order_state(value):
    """Canonicalize an order's state (casing + misspellings) but keep the TRUE state —
    NO Telangana->AP fold, since orders are revenue. Falls back to the raw value when
    nothing canonicalizes (e.g. junk text with no match) so we never null a state.
    Lazy import dodges the leadService<->orders circular import."""
    from services.leadService import canon_state
    return canon_state(value, apply_alias=False) or value

logger = logging.getLogger(__name__)

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

    is_crm=True (order created by a telecaller/proxy) gets the ``ORD-CRM-``
    prefix so the daily-rev split (CRM vs outlet manager) works; outlet-manager
    orders stay ``ORD-``. Callers decide is_crm from the creator's role, NOT
    lead_id — every order hangs off a lead now, so lead_id can't tell them apart.
    Mirrors the legacy LSQ webhook prefix (crm.py) so both CRM origins share
    ``ORD-CRM-``.
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


async def _build_order_filters(
    ctx,
    *,
    transfer_status: Optional[OrderStatus] = None,
    telecaller_id: Optional[str] = None,
    outlet_id: Optional[str] = None,
    state: Optional[str] = None,
    customer_phone: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    dynamic_filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Shared filter-building for GET /orders and GET /orders/export, so the export
    matches the screen exactly (same params, same scope) instead of the two drifting
    apart over time. Extracted verbatim from get_orders -- see there for the original
    per-step rationale (scope, admin-only cross-cutting filters, IST date ranges,
    dynamic-filter date coercion)."""
    dynamic_filters = dict(dynamic_filters or {})

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

    # 3. Date range filters. order_date is timestamptz — build the range in IST
    # so a picked calendar day means that day in IST, not UTC.
    if from_date or to_date:
        gte, lte = ist_range_bounds(from_date, to_date)
        date_filter = {}
        if gte is not None:
            date_filter[">="] = gte
        if lte is not None:
            date_filter["<="] = lte
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
    return filters


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
        
        # ORD-CRM- for telecaller-created (CRM) orders, plain ORD- for outlet-manager
        # (outlet-sales) orders. Every order now hangs off a lead, so lead_id no longer
        # tells them apart — key on the creator's role instead.
        order_number = generate_order_number(is_crm=ctx.role in _ORDER_OWN_CREATED_ROLES)

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
            state=_canon_order_state(payload.state),
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

        await order_events_service.record_event(
            created_order, OrderEventType.CREATED,
            actor_id=ctx.user_id, source="erp", status=created_order.order_status,
        )

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

                # Lead ownership (T5.1): the booking telecaller becomes Lead Owner.
                await leadService.reassign_to_booker(engine, payload.lead_id, current_user_id, ctx.role)

            except Exception:
                # Deliberate: a CRM-side failure must never break order creation (the order
                # already committed above). Log loudly with context instead of silently
                # swallowing it — this used to be a bare print(), which is the prime
                # suspect for "lead stage not changing after order booking" reports.
                logger.exception(
                    "Post-order CRM block failed for lead %s (order %s / %s)",
                    payload.lead_id, created_order.uid, order_number,
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
        
        # Step 6: Create order (attributed to telecaller, not admin). Proxy orders are
        # always telecaller-attributed CRM orders -> ORD-CRM- prefix.
        order_number = generate_order_number(is_crm=True)

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
            state=_canon_order_state(payload.state),
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

                # Lead ownership (T5.1): proxy orders book AS the target telecaller, not the
                # admin caller — reassign using their id/role, not current_user_id/ctx.role.
                await leadService.reassign_to_booker(engine, payload.lead_id, payload.telecaller_id, target_telecaller.role)

            except Exception:
                # Deliberate: a CRM-side failure must never break order creation (the order
                # already committed above). Log loudly with context instead of silently
                # swallowing it (see create_order's identical block for why).
                logger.exception(
                    "Post-order CRM block failed for lead %s (proxy order %s / %s)",
                    payload.lead_id, created_order.uid, created_order.order_number,
                )
        
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

                # Mutual exclusion: an order under CRM review belongs to CRM, not logistics.
                state = fold_order_state(await order_events_service.load_events(order_id))
                if state.escalation_state == EscalationState.CRM_REVIEW:
                    results.append(BulkAssignmentResult(
                        order_id=order_id,
                        status="failed",
                        message="Order is under CRM review and cannot be assigned"
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
                    changed_by=current_user_id,
                    source="erp",
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

MAX_BULK_STATUS_ROWS = 1000  # ponytail: sync loop; move to a background job if real batches outgrow this


@router.post("/bulk/status-update", response_model=BulkOrderStatusUpdateResponse)
async def bulk_update_order_status(
    payload: BulkOrderStatusUpdateRequest,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_REVOKE)),
):
    """Guarded bulk status update (SUPER_ADMIN via orders:revoke).

    File is parsed client-side; body carries the order numbers. confirm_text
    must equal the target status name — the typed guard is enforced server-side.
    Transitions follow the single-endpoint rules plus CANCELLED -> PENDING
    (un-cancel). DELIVERED orders are never modified. Bad rows are skipped with
    a reason; the batch never aborts. Each applied change also lands on the
    linked lead's activity timeline via _apply_status_change.
    """
    try:
        if payload.confirm_text.strip().lower() != payload.order_status.value.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Confirmation text must match the status name '{payload.order_status.value}'"
            )
        if payload.order_status == OrderStatus.DELIVERED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Bulk update cannot mark orders delivered; use the single-order flow."
            )
        if payload.order_status not in (OrderStatus.DELIVERED, OrderStatus.DELIVERY_ALLOTTED) \
                and not payload.status_remarks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Status remarks required for {payload.order_status.value}"
            )

        # normalize + dedupe, preserving file order
        numbers, seen = [], set()
        for raw in payload.order_numbers:
            n = (raw or "").strip().upper()
            if n and n not in seen:
                seen.add(n)
                numbers.append(n)
        if not numbers:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No order numbers provided")
        if len(numbers) > MAX_BULK_STATUS_ROWS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Too many rows: {len(numbers)} (max {MAX_BULK_STATUS_ROWS})"
            )

        found = await order_manager.fetch_all(
            filters={"order_number": numbers},
            joins=[
                (CustomerOrderSchema.assigned_outlet, OutletSchema.manager),
                (CustomerOrderSchema.items, OrderItemSchema.product)
            ],
        )
        by_number = {o.order_number: o for o in found.items}

        updated, skipped = 0, []
        for n in numbers:
            order = by_number.get(n)
            if order is None:
                skipped.append(BulkOrderStatusUpdateResult(order_number=n, reason="order not found"))
                continue
            if order.order_status == OrderStatus.DELIVERED:
                skipped.append(BulkOrderStatusUpdateResult(
                    order_number=n, reason="already delivered — bulk cannot modify delivered orders"))
                continue
            try:
                await _apply_status_change(
                    order, payload.order_status, payload.status_remarks,
                    None, ctx.user_id, background_tasks, allow_uncancel=True,
                )
                updated += 1
            except HTTPException as row_err:
                skipped.append(BulkOrderStatusUpdateResult(order_number=n, reason=str(row_err.detail)))
            except Exception as row_err:
                print(f"⚠️ Bulk status update failed for order {n}: {row_err}")
                skipped.append(BulkOrderStatusUpdateResult(order_number=n, reason=f"failed: {row_err}"))

        return BulkOrderStatusUpdateResponse(total=len(numbers), updated=updated, skipped=skipped)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to bulk update order status: {str(e)}"
        )


MAX_BULK_REASSIGN_ROWS = 2000


@router.post("/bulk/reassign", response_model=BulkOrderReassignResponse)
async def bulk_reassign_orders(
    payload: BulkOrderReassignRequest,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_REVOKE)),
):
    """Bulk reassign orders to suggested outlets (matching transfer_orders_to_suggested.py logic).

    Rules:
      - Deduplicates items (last-wins per order_number).
      - Terminal orders (DELIVERED, CANCELLED) are skipped.
      - If order is already assigned to the suggested outlet, counts as already_assigned.
      - If order is past PENDING, it is reset to PENDING and its rider / delivery date cleared.
      - If order is PENDING, assigned_outlet_id is updated.
    """
    try:
        if not payload.items:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No items provided")
        if len(payload.items) > MAX_BULK_REASSIGN_ROWS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Too many items: {len(payload.items)} (max {MAX_BULK_REASSIGN_ROWS})"
            )

        # Deduplicate items: last occurrence wins (same as script logic)
        seen = {}
        for item in payload.items:
            order_no = (item.order_number or "").strip()
            target_outlet = (item.suggested_outlet or "").strip()
            if order_no and target_outlet:
                seen[order_no.upper()] = (order_no, target_outlet)

        deduped = list(seen.values())
        if not deduped:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid order entries found")

        # Fetch all outlets to build lookup maps (name, code, uid)
        all_outlets_res = await outlet_manager.fetch_all(limit=1000, offset=0)
        outlets_list = all_outlets_res.items if hasattr(all_outlets_res, "items") else (all_outlets_res or [])

        outlet_by_key = {}
        for o in outlets_list:
            uid = getattr(o, "uid", None) or getattr(o, "id", None)
            name = (getattr(o, "outlet_name", None) or getattr(o, "name", "") or "").strip().lower()
            code = (getattr(o, "outlet_code", None) or getattr(o, "code", "") or "").strip().lower()
            is_active = getattr(o, "is_active", True)

            val = {
                "uid": uid,
                "name": getattr(o, "outlet_name", None) or getattr(o, "name", ""),
                "is_active": is_active,
            }
            if uid:
                outlet_by_key[str(uid).lower()] = val
            if name:
                outlet_by_key[name] = val
            if code:
                outlet_by_key[code] = val

        # Fetch target orders by order_number
        order_numbers = [orig_no for orig_no, _ in deduped]
        found_orders_res = await order_manager.fetch_all(
            filters={"order_number": order_numbers},
            limit=len(order_numbers) + 10,
        )
        found_orders = found_orders_res.items if hasattr(found_orders_res, "items") else []
        order_by_number = {
            o.order_number.upper(): o for o in found_orders if getattr(o, "order_number", None)
        }

        terminal_statuses = {OrderStatus.DELIVERED, OrderStatus.CANCELLED}

        reassigned = 0
        already_assigned = 0
        reset_to_pending = 0
        skipped = []

        for orig_no, target_outlet_str in deduped:
            key_no = orig_no.upper()
            order = order_by_number.get(key_no)
            if not order:
                skipped.append(
                    BulkOrderReassignResult(
                        order_number=orig_no,
                        suggested_outlet=target_outlet_str,
                        reason="order not found",
                    )
                )
                continue

            target_info = outlet_by_key.get(target_outlet_str.strip().lower())
            if not target_info:
                skipped.append(
                    BulkOrderReassignResult(
                        order_number=orig_no,
                        suggested_outlet=target_outlet_str,
                        reason=f"outlet '{target_outlet_str}' not found",
                    )
                )
                continue

            if not target_info["is_active"]:
                skipped.append(
                    BulkOrderReassignResult(
                        order_number=orig_no,
                        suggested_outlet=target_outlet_str,
                        reason=f"outlet '{target_info['name']}' is inactive",
                    )
                )
                continue

            target_uid = target_info["uid"]
            current_status = getattr(order, "order_status", None) or getattr(order, "status", None)

            if current_status in terminal_statuses:
                status_str = current_status.value if hasattr(current_status, "value") else str(current_status)
                skipped.append(
                    BulkOrderReassignResult(
                        order_number=orig_no,
                        suggested_outlet=target_outlet_str,
                        reason=f"order is terminal ({status_str})",
                    )
                )
                continue

            if order.assigned_outlet_id == target_uid:
                already_assigned += 1
                continue

            is_past_pending = current_status != OrderStatus.PENDING
            update_fields = {"assigned_outlet_id": target_uid}

            if is_past_pending:
                update_fields["order_status"] = OrderStatus.PENDING
                update_fields["delivery_person_id"] = None
                update_fields["actual_delivery_date"] = None
                reset_to_pending += 1

            if not payload.dry_run:
                await order_manager.update(order.uid, update_fields)
            reassigned += 1

        return BulkOrderReassignResponse(
            total=len(deduped),
            reassigned=reassigned,
            already_assigned=already_assigned,
            reset_to_pending=reset_to_pending,
            skipped=skipped,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to bulk reassign orders: {str(e)}"
        )


# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

@router.get("/export")
async def export_orders(
    transfer_status: Optional[OrderStatus] = None,
    telecaller_id: Optional[str] = None,
    outlet_id: Optional[str] = None,
    state: Optional[str] = None,
    customer_phone: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    dynamic_filters: Dict[str, Any] = Depends(D.filtering_dependency),
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_READ, allow_scopes=["delivery:read"])),
):
    """
    Streamed, server-side CSV export of orders. SAME filters and SAME scoping as
    GET /orders (via the shared `_build_order_filters`), so the export never leaks a
    row the caller couldn't already see on the report screen.

    Replaces the superadmin report's client-side "Export Excel" (which required the
    grid to load with `limit=0` / LIMIT: All to have every row in the browser first --
    see get_orders's docstring/comments on why that path is gone). This streams
    keyset-paginated batches straight to CSV instead; registered ahead of
    GET /{order_id} so "export" is never swallowed as a path parameter.
    """
    filters = await _build_order_filters(
        ctx,
        transfer_status=transfer_status,
        telecaller_id=telecaller_id,
        outlet_id=outlet_id,
        state=state,
        customer_phone=customer_phone,
        from_date=from_date,
        to_date=to_date,
        dynamic_filters=dynamic_filters,
    )
    filename = f"orders_{ist_today()}.csv"
    return StreamingResponse(
        stream_orders_csv(filters),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.ORDERS_READ, allow_scopes=["delivery:read"])),
):
    """Get specific order details"""
    try:
        order = await order_manager.fetch(order_id, joins = [
            CustomerOrderSchema.items, CustomerOrderSchema.telecaller,
            CustomerOrderSchema.assigned_outlet, CustomerOrderSchema.delivery_person,
        ])

        await _assert_order_in_scope(ctx, order)
        data = order.model_dump()
        # delivery_person is typed `dict` on OrderResponse, so pydantic won't filter it —
        # slim it by hand or the whole user row (password hash included) ships to the client.
        if data.get("delivery_person"):
            data["delivery_person"] = {k: data["delivery_person"].get(k) for k in ("full_name", "phone")}
        return apply_field_mask("orders", ctx, data)
    
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
                        "last_updated": datetime.now(timezone.utc)
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
                        "last_updated": datetime.now(timezone.utc)
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

        filters = await _build_order_filters(
            ctx,
            transfer_status=transfer_status,
            telecaller_id=telecaller_id,
            outlet_id=outlet_id,
            state=state,
            customer_phone=customer_phone,
            from_date=from_date,
            to_date=to_date,
            dynamic_filters=dynamic_filters,
        )

        orders = await order_manager.fetch_all(
            filters=filters,
            joins=joins,
            limit=limit,
            offset=offset,
            sorts=sorts or ["-created_at"],
        )

        # Filtered TOTAL for real server-side pagination (Page X of Y), additive
        # alongside `count` (page length) so existing consumers (outlet order views)
        # keep working untouched. Mirrors GET /leads' `total`: a plain COUNT over the
        # SAME filters, on the base table -- get_orders_count never adds the
        # items/product join `joins` above does, so it can't be inflated by that
        # one-to-many fan-out the way a naive COUNT(*) over the joined query would be.
        total = await order_manager.get_orders_count(filters=filters)

        result = orders.model_dump()
        items = apply_field_mask("orders", ctx, result.get("items", []))

        # Attach who/from-where each order's status was last changed (the report export's
        # rider/actor + ERP-vs-rider-app columns) AND attempt_count (the SA export's /
        # outlet order view's "attempt count" column) via TWO lean, 1-row-per-order
        # queries: tracking_manager.latest_by_order (DISTINCT ON) and
        # order_events_service.attempt_counts_for (SQL COUNT aggregate). Both chunk their
        # IN-list internally. This used to be consolidated into ONE load_events_bulk() +
        # fold_order_state() scan ("one query instead of two"), which is fine at page
        # scale but fetches (and Python-folds) EVERY delivery_tracking event for EVERY
        # order when limit=0 (LIMIT: All) — the SA export's "fetch everything" mode —
        # which blows past asyncpg's bind-param cap and never returns. Restored the lean
        # pair; /orders/returns and /orders/crm-queue still use load_events_bulk +
        # fold_order_state directly (bounded candidate sets, need full folded state).
        uids = [it.get("uid") for it in items if it.get("uid")]
        latest = await tracking_manager.latest_by_order(uids)
        attempt_counts = await order_events_service.attempt_counts_for(uids)

        for it in items:
            info = latest.get(it.get("uid")) or {}
            it["last_status_source"] = info.get("source")
            it["last_status_changed_by_id"] = info.get("changed_by")
            it["last_status_changed_by_name"] = info.get("changed_by_name")
            it["attempt_count"] = attempt_counts.get(it.get("uid"), 0)

        result["items"] = items
        result["total"] = total
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
            update_data["state"] = _canon_order_state(payload.state)
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


async def _apply_status_change(
    order,
    new_status: OrderStatus,
    remarks: Optional[str],
    postpone_date: Optional[date],
    user_id: str,
    background_tasks: BackgroundTasks,
    *,
    allow_uncancel: bool = False,
) -> None:
    """Status-change engine shared by the single and bulk endpoints.

    Validates the transition, applies the status-specific side effects
    (store sync, CRM push, stock, delivery tracking), persists the update and
    logs the change on the linked lead's timeline. Raises HTTPException(400)
    on an invalid transition or missing remarks.
    """
    old_status = order.order_status

    # Mutual exclusion: an order under CRM review belongs to CRM, not logistics.
    # Block any logistics-driven status change (assign/cancel/postpone/etc.) on it —
    # CRM resolves it via the CRM queue (crm-outcome), which does not go through here.
    # ponytail: one fold per status change; fine at this call rate.
    _crm_state = fold_order_state(await order_events_service.load_events(order.uid))
    if _crm_state.escalation_state == EscalationState.CRM_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order is under CRM review and cannot be changed by logistics. Resolve it via the CRM queue.",
        )

    # Validate status transition
    if not _transition_allowed(order.order_status, new_status, allow_uncancel=allow_uncancel):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status transition from {order.order_status} to {new_status}"
        )

    # Handle status-specific logic
    update_data = {
        "order_status": new_status,
        "status_remarks": remarks
    }

    update_order = lambda order_obj, update_dict: [setattr(order_obj, k, v) for k, v in update_dict.items()]

    if new_status == OrderStatus.DELIVERED:
        # actual_delivery_date is a plain DATE column in prod (not timestamptz,
        # despite the ORM's DateTime(timezone=True) declaration) — write the IST
        # calendar day, not a UTC-naive datetime Postgres would truncate wrong
        # for deliveries between IST midnight and 05:30 IST.
        update_data["actual_delivery_date"] = ist_today()

        # Update local object so model_dump() picks it up for CRM
        update_order(order, update_data)

        # push the order status to store
        await store_service.order_delivered(order.uid)

        # push activity to crm
        background_tasks.add_task(sync_order_to_crm, engine, order.uid, ActivityType.DELIVERY_STATUS)
        background_tasks.add_task(trigger_smartping_event_bg, order.uid, "order_delivered")

        # Consume stock from inventory
        await consume_order_stock(order.uid)

    elif new_status == OrderStatus.CANCELLED:
        if not remarks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Status remarks required for cancelled orders"
            )

        update_order(order, update_data)

        # push the order status to store
        await store_service.order_cancelled(order.uid)

        # push activity to crm
        background_tasks.add_task(sync_order_to_crm, engine, order.uid, ActivityType.ORDER_STATUS)
        # Stock reservation logic removed

    elif new_status == OrderStatus.DELIVERY_ALLOTTED:
        # actual_delivery_date is a plain DATE column in prod (not timestamptz,
        # despite the ORM's DateTime(timezone=True) declaration) — write the IST
        # calendar day, not a UTC-naive datetime Postgres would truncate wrong
        # for deliveries between IST midnight and 05:30 IST.
        update_data["actual_delivery_date"] = ist_today()

        # Update local object so model_dump() picks it up for CRM
        update_order(order, update_data)

        # push the order status to store
        await store_service.order_fulfilled(order.uid)

        # push activity to crm
        background_tasks.add_task(sync_order_to_crm, engine, order.uid, ActivityType.DELIVERY_STATUS)
        background_tasks.add_task(trigger_smartping_event_bg, order.uid, "order_dispatched")

    elif new_status == OrderStatus.PENDING:
        if not remarks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Status remarks required when reverting to pending"
            )

        update_data["delivery_person_id"] = None
        update_data["actual_delivery_date"] = None

        update_order(order, update_data)
        # Tracking row + lead activity are written once, below, for every transition.

    elif new_status in [
        OrderStatus.POSTPONED,
        OrderStatus.ATTEMPTED,
        OrderStatus.CUSTOMER_NOT_AVAILABLE,
        OrderStatus.UNABLE_TO_CONTACT,
        OrderStatus.UNABLE_TO_LOCATE,
        OrderStatus.PAYMENT_NOT_READY
    ]:
        if not remarks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Status remarks required for {new_status.value}"
            )

        update_data["priority_level"] = (order.priority_level or 0) + 10

        if new_status in [OrderStatus.POSTPONED, OrderStatus.PAYMENT_NOT_READY] and postpone_date:
            update_data["expected_delivery_date"] = postpone_date
        else:
            update_data["expected_delivery_date"] = datetime.utcnow().date() + timedelta(days=1)

        update_order(order, update_data)

        # push activity to crm
        background_tasks.add_task(sync_order_to_crm, engine, order.uid, ActivityType.DELIVERY_STATUS)

    await order_manager.update(order.uid, update_data)

    # Audit EVERY transition in delivery_tracking (source='erp'). Best-effort: the
    # status update is already committed, so a tracking-write failure must never 500
    # the request. Skipped when the order lacks the NOT NULL outlet/telecaller.
    # ponytail: one row per change; the daily revert job reads the latest to time out.
    if order.assigned_outlet_id and order.telecaller_id:
        try:
            existing_tracking = await tracking_manager.fetch_all(
                filters={"order_id": order.uid}, sorts=["created_at"]
            )
            base_remark = (f"Order unassigned. Reason: {remarks}"
                           if new_status == OrderStatus.PENDING else remarks)
            await tracking_manager.create(DeliveryTrackingSchema(
                order_id=order.uid,
                outlet_id=order.assigned_outlet_id,
                telecaller_id=order.telecaller_id,
                delivery_person_id=(None if new_status == OrderStatus.PENDING
                                    else order.delivery_person_id),
                status_changed_to=new_status,
                event_type=OrderEventType.STATUS_CHANGE,
                postpone_date=postpone_date,
                priority_level=order.priority_level or 0,
                remarks=build_cumulative_remarks(existing_tracking.items, new_status, base_remark),
                changed_by=user_id,
                source="erp",
            ))
        except Exception as track_err:
            print(f"WARNING: tracking row not written for {order.uid}: "
                  f"{type(track_err).__name__}: {track_err}")

    # Internal CRM: log the status change on the lead timeline, attributed
    # to the user who made the change.
    from services import leadService
    background_tasks.add_task(
        leadService.log_order_status_change, engine, order,
        new_status, user_id,
        old_status=old_status, remarks=remarks,
    )


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

        await _apply_status_change(
            order, payload.order_status, payload.status_remarks,
            payload.postpone_date, ctx.user_id, background_tasks,
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
                    "last_updated": datetime.now(timezone.utc)
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
                    changed_by=current_user_id,
                    source="erp",
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

                # Snapshot the order into the event log BEFORE the hard delete — the
                # event row has no FK to the order (Task 2), so it survives. Written
                # into the SAME session as the delete below so both commit atomically:
                # if the delete fails, the snapshot rolls back too (no orphaned DELETED
                # event for an order that still exists).
                snapshot = {
                    "order_number": order_to_delete.order_number,
                    "customer_name": order_to_delete.customer_name,
                    "customer_phone": order_to_delete.customer_phone,
                    "final_status": str(order_to_delete.order_status),
                    "total_amount": float(order_to_delete.total_amount or 0),
                    "assigned_outlet_id": order_to_delete.assigned_outlet_id,
                    "deleted_reason": reason,
                }
                await order_events_service.record_event(
                    order_to_delete, OrderEventType.DELETED,
                    actor_id=ctx.user_id, source="erp", payload=snapshot,
                    session=session,
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
                                "last_updated": datetime.now(timezone.utc)
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
