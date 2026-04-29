from typing import Optional
from datetime import datetime
from decimal import Decimal
from utils.constants import ActivityType
from services import CRMService
from managers import CustomerOrderManager, CustomerOrderSchema, OrderItemSchema, OutletSchema

async def sync_order_to_crm(engine, order_id: str, activity_event: ActivityType):
    """
    Background task to sync order activity to LeadSquared.
    Fetches the order with all necessary relationships and pushes to CRM.
    """
    crm_service = CRMService()
    order_manager = CustomerOrderManager(engine)
    
    try:
        # Fetch order with items, products, and assigned outlet info
        order = await order_manager.fetch(order_id, joins=[
            (CustomerOrderSchema.items, OrderItemSchema.product),
            (CustomerOrderSchema.assigned_outlet, OutletSchema.manager),
            (CustomerOrderSchema.telecaller)
        ])
        
        if not order:
            print(f"[ERROR] CRM Sync Failed: Order {order_id} not found")
            return

        # Build consistent payload
        payload = build_crm_payload(order)
        
        result = await crm_service.push_activity({
            "order_data": payload,
            "activity_event": activity_event
        })
        print(f"[SUCCESS] CRM {activity_event} Sync Result: {result}")
    except Exception as e:
        print(f"[ERROR] CRM {activity_event} Sync Failed for order {order_id}: {str(e)}")

def build_crm_payload(order: CustomerOrderSchema):
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
                "unit_price": float(item.product.unit_price)
            },
            "quantity": item.quantity,
            "unit_price": float(item.unit_price),
            "subtotal": float(item.subtotal),
            "product_manual_discount": float(item.product_manual_discount),
            "discount_amount": float(item.discount_amount or 0)
        })
    
    # Map status for CRM (LeadSquared expects specific strings sometimes)
    status_str = order.order_status.value if hasattr(order.order_status, "value") else str(order.order_status)
    # If it's a create event, LeadSquared mapping might expect "Active" or similar, 
    # but we'll send the actual status and let CRMService handle it.
    
    payload = {
        "order_id": order.uid,
        "order_number": order.order_number,
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "pincode": order.pincode,
        "address_line": order.address_line,
        "city": order.district, # LeadSquared often uses 'city' for district
        "state": order.state,
        "order_status": status_str,
        "collection_type": order.collection_type.value if hasattr(order.collection_type, "value") else str(order.collection_type),
        "payment_method": order.payment_method.value if hasattr(order.payment_method, "value") else str(order.payment_method),
        "prepaid_amount": float(order.prepaid_amount),
        "grand_total": float(order.total_amount),
        "total_amount": float(order.total_amount),
        "discount_applied": float(order.discount_applied),
        "status_remarks": order.status_remarks,
        "created_at": order.order_date.isoformat() if order.order_date else None,
        "actual_delivery_date": order.actual_delivery_date.isoformat() if order.actual_delivery_date else None,
        "items": items
    }
    
    # Determine delivery status
    delivery_status_str = "not_assigned"
    if status_str == "delivered":
        delivery_status_str = "delivered"
    elif status_str == "cancelled":
        delivery_status_str = "cancelled"
    elif order.delivery_person_id or order.assigned_outlet_id or status_str == "delivery_allotted":
        delivery_status_str = "assigned"
        
    payload["delivery_status"] = delivery_status_str
    
    # Include outlet info if assigned
    if order.assigned_outlet:
        payload["assigned_outlet"] = {
            "outlet_name": order.assigned_outlet.outlet_name,
            "address": order.assigned_outlet.address,
            "phone": order.assigned_outlet.phone,
            "manager": {
                "full_name": order.assigned_outlet.manager.full_name if order.assigned_outlet.manager else None,
                "phone": order.assigned_outlet.manager.phone if order.assigned_outlet.manager else None
            }
        }
        
    # Include source/telecaller info
    if order.telecaller:
        payload["source"] = order.telecaller.role.value if hasattr(order.telecaller.role, "value") else str(order.telecaller.role)
    
    return payload
