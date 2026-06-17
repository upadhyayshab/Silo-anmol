from typing import Optional
from datetime import datetime
from decimal import Decimal
try:
    from sqlalchemy.exc import DetachedInstanceError
except ImportError:
    from sqlalchemy.orm.exc import DetachedInstanceError
from utils.constants import UserRole
from utils.crm_constants import ActivityType, LSQUTMField, LeadSource
from services import CRMService
from managers import (
    CustomerOrderManager, CustomerOrderSchema,
    OrderItemSchema, OutletSchema,
)


def _safe_rel(obj, attr: str):
    """Return a relationship attribute value, or None if the session is closed."""
    try:
        return getattr(obj, attr)
    except DetachedInstanceError:
        return None


# build_crm_payload accesses all four relationships for every activity type,
# so every spec must load all four joins.
_ALL_ORDER_JOINS = [
    (CustomerOrderSchema.items, OrderItemSchema.product),
    (CustomerOrderSchema.assigned_outlet, OutletSchema.manager),
    CustomerOrderSchema.telecaller,
    CustomerOrderSchema.delivery_person,
]

ACTIVITY_FETCH_SPECS: dict[ActivityType, list] = {
    ActivityType.DELIVERY_STATUS: _ALL_ORDER_JOINS,
    ActivityType.ORDER_STATUS:    _ALL_ORDER_JOINS,
    ActivityType.PAYMENT_STATUS:  _ALL_ORDER_JOINS,
    ActivityType.CREATE_ORDER:    _ALL_ORDER_JOINS,
}


async def sync_order_to_crm(engine, order_id: str, activity_event: ActivityType):
    """
    Background task to sync order activity to LeadSquared.
    Fetches only the joins required for the given activity type.
    """
    crm_service = CRMService()
    order_manager = CustomerOrderManager(engine)

    try:
        joins = ACTIVITY_FETCH_SPECS.get(activity_event, [])
        order = await order_manager.fetch(order_id, joins=joins)

        if not order:
            print(f"[ERROR] CRM Sync Failed: Order {order_id} not found")
            return

        payload = build_crm_payload(order)

        result = await crm_service.push_activity(payload, activity_event)
        print(f"[SUCCESS] CRM {activity_event} Sync Result: {result}")
    except Exception as e:
        print(f"[ERROR] CRM {activity_event} Sync Failed for order {order_id}: {str(e)}")


def build_crm_payload(order: CustomerOrderSchema) -> dict:
    """
    Builds a consistent payload dictionary from a CustomerOrderSchema object.
    Matches the structure expected by CRMService.build_payload.
    """
    items = []
    for item in order.items:
        items.append({
            "product": {
                "product_name": item.product.product_name,
                "lsq_display_name": item.product.lsq_display_name,
                "unit_of_measure": item.product.unit_of_measure,
                "cost_price": float(item.product.cost_price),
                "unit_price": float(item.product.unit_price),
            },
            "quantity": item.quantity,
            "unit_price": float(item.unit_price),
            "subtotal": float(item.subtotal),
            "product_manual_discount": float(item.product_manual_discount),
            "discount_amount": float(item.discount_amount or 0),
        })

    status_str = order.order_status.value if hasattr(order.order_status, "value") else str(order.order_status)

    if order.total_amount <= 0:
        payment_status = "paid"
    elif order.prepaid_amount > 0:
        payment_status = "partially_paid"
    else:
        payment_status = "pending"

    payload = {
        "order_id":       order.uid,
        "order_number":   order.order_number,
        "customer_name":  order.customer_name,
        "customer_phone": order.customer_phone,
        "pincode":        order.pincode,
        "address_line":   order.address_line,
        "city":           order.district,
        "state":          order.state,
        "order_status":   status_str,
        "payment_status": payment_status,
        "collection_type": order.collection_type.value if hasattr(order.collection_type, "value") else str(order.collection_type),
        "payment_method":  order.payment_method.value if hasattr(order.payment_method, "value") else str(order.payment_method),
        "prepaid_amount":  float(order.prepaid_amount),
        "grand_total":     float(order.total_amount),
        "total_amount":    float(order.total_amount),
        "discount_applied": float(order.discount_applied),
        "status_remarks":  order.status_remarks,
        "created_at":      order.order_date.isoformat() if order.order_date else None,
        "actual_delivery_date": order.actual_delivery_date.isoformat() if order.actual_delivery_date else None,
        "items": items,
    }

    delivery_status_str = "not_assigned"
    if status_str == "delivered":
        delivery_status_str = "delivered"
    elif status_str == "cancelled":
        delivery_status_str = "cancelled"
    elif status_str == "delivery_allotted":
        delivery_status_str = "delivery_allotted"
    elif order.delivery_person_id or order.assigned_outlet_id:
        delivery_status_str = "assigned"
    payload["delivery_status"] = delivery_status_str

    assigned_outlet  = _safe_rel(order, "assigned_outlet")
    delivery_person  = _safe_rel(order, "delivery_person")
    telecaller       = _safe_rel(order, "telecaller")

    if assigned_outlet:
        manager = _safe_rel(assigned_outlet, "manager")
        payload["assigned_outlet"] = {
            "outlet_name": assigned_outlet.outlet_name,
            "address":     assigned_outlet.address,
            "phone":       assigned_outlet.phone,
            "manager": {
                "full_name": manager.full_name if manager else None,
                "phone":     manager.phone if manager else None,
            },
        }

    payload["delivery_guy"] = (
        {"full_name": delivery_person.full_name, "phone": delivery_person.phone}
        if delivery_person else None
    )

    if telecaller:
        role_val = telecaller.role.value if hasattr(telecaller.role, "value") else str(telecaller.role)
        payload["source"] = role_val
        # An order created by an outlet manager is captured at an outlet, so both
        # LSQ sources are tagged "Outlet": the prospect's lead source ("Source")
        # and the order activity's source (lowercase "source").
        if role_val == UserRole.OUTLET_MANAGER.value:
            payload["source"] = LeadSource.OUTLET.value
            payload["Source"] = LeadSource.OUTLET.value

    payload["delivery_remarks"] = delivery_status_str

    return payload


def map_lsq_utm_data(raw_data: dict) -> Optional[dict]:
    """
    Maps raw LeadSquared custom object fields (mx_CustomObject_X)
    to human-readable keys based on LSQUTMField mapping.
    """
    if not raw_data or not isinstance(raw_data, dict):
        return None

    mapping = {f.value: f.name.lower() for f in LSQUTMField}
    mapped_data = {readable: raw_data.get(lsq_key) for lsq_key, readable in mapping.items()}
    return mapped_data if any(mapped_data.values()) else None
