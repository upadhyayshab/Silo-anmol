from fastapi import APIRouter, HTTPException, Depends, status, Body, Path, Query, BackgroundTasks
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, date
from decimal import Decimal
import uuid
import logging
import httpx

from config import get_settings, get_engine
from managers import (
    CustomerOrderManager, OrderItemManager, OrderTransactionManager,
    InventoryManager, ProductManager, OutletManager, UserManager,
    CustomerOrderSchema, OrderItemSchema, OrderTransactionSchema,
    ActivityLogManager, ActivityLogSchema
)
from utils.constants import UserRole, OrderStatus, PaymentStatus, CollectionType, PaymentMethod

settings = get_settings()
engine = get_engine(settings.name)

order_manager = CustomerOrderManager(engine)
order_item_manager = OrderItemManager(engine)
transaction_manager = OrderTransactionManager(engine)
inventory_manager = InventoryManager(engine)
product_manager = ProductManager(engine)
outlet_manager = OutletManager(engine)
user_manager = UserManager(engine)
activity_manager = ActivityLogManager(engine)

router = APIRouter(prefix="/crm", tags=["CRM"])


class CRMItem(BaseModel):
    product_id: str = Field(..., description="UUID of the product")
    quantity: int = Field(default=1, ge=1)


class CRMOrderPayload(BaseModel):
    customer_name: str
    customer_phone: str
    house_no: Optional[str] = None
    street: Optional[str] = None
    address_line: str
    village: Optional[str] = None
    post: Optional[str] = None
    hobli: Optional[str] = None
    taluk: Optional[str] = None
    district: str
    state: str
    pincode: str
    items: List[CRMItem]


@router.post("/webhook")
async def webhook(background_tasks: BackgroundTasks, payload: dict = Body(None, openapi_examples={
    "new_order": {
        "summary": "New CRM Order",
        "description": "Standard payload for a new order received from external CRM.",
        "value": {
            "customer_name": "Jane Doe",
            "customer_phone": "9876543210",
            "address_line": "3rd main street",
            "district": "Bangalore",
            "state": "Karnataka",
            "pincode": "573001",
            "items": [
                {"product_id": "product-uuid-1", "quantity": 1},
                {"product_id": "product-uuid-2", "quantity": 2}
            ]
        }
    }
})):
    """Receives webhook from CRM and processes it in background"""
    background_tasks.add_task(process_crm_orders, payload)
    return {"message": "Webhook received, processing in background"}


async def auto_assign_outlet(district: str, state: str, taluk: str = None) -> Optional[object]:
    """Auto-assign outlet based on Google Sheets mapping"""
    try:
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
            
            if response.status_code != 200:
                print(f"❌ Google API error: {response.status_code}")
                return None
            
            try:
                api_data = response.json()
            except Exception as json_error:
                print(f"❌ JSON parsing error: {json_error}")
                return None
            
            if not api_data.get("success"):
                return None
            
            # Get outlet name from API response
            api_outlet_name = api_data.get("outlet", "").strip()
            if not api_outlet_name:
                return None
            
            # Get all active outlets from database
            outlets = await outlet_manager.fetch_all(
                filters={"is_active": True}
            )
            
            if not outlets.items:
                return None
            
            # Match outlet using first word comparison (case-insensitive)
            api_first_word = api_outlet_name.lower().split()[0] if api_outlet_name else ""
            
            matched_outlet = None
            for outlet in outlets.items:
                outlet_first_word = outlet.outlet_name.lower().split()[0] if outlet.outlet_name else ""
                
                if api_first_word == outlet_first_word:
                    matched_outlet = outlet
                    break
            
            return matched_outlet
            
    except Exception as e:
        print(f"❌ Error in Google outlet assignment: {str(e)}")
        return None


async def reserve_crm_stock(order_id: str, outlet_id: str, validated_items: List[dict]):
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
                logging.warning(f"No stock available for product {item_data['product'].product_name}")
                continue
            
            inventory_item = warehouse_inventory.items[0]
        else:
            inventory_item = inventory_items.items[0]
        
        # Check available stock
        available = inventory_item.quantity - inventory_item.reserved_quantity
        if available < quantity:
            logging.warning(f"Insufficient stock for {item_data['product'].product_name}. Available: {available}, Required: {quantity}")
            continue
        
        # Reserve stock
        new_reserved = inventory_item.reserved_quantity + quantity
        await inventory_manager.update(
            inventory_item.uid,
            {
                "reserved_quantity": new_reserved,
                "last_updated": datetime.utcnow()
            }
        )


def generate_order_number() -> str:
    """Generate unique order number"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"ORD-CRM-{timestamp}-{str(uuid.uuid4())[:8].upper()}"


async def process_crm_orders(payload: dict):
    """Background task to process CRM orders"""
    try:
        print(f"📦 Processing CRM Order: {payload}")
        
        # 1. Validate mandatory fields
        customer_name = payload.get("customer_name")
        customer_phone = payload.get("customer_phone")
        address_line = payload.get("address_line", "")
        district = payload.get("district")
        state = payload.get("state")
        pincode = payload.get("pincode")
        items_data = payload.get("items", [])
        
        if not all([customer_name, customer_phone, district, state, items_data]):
            print("❌ Missing mandatory fields in CRM payload")
            return

        # 2. Resolve items and calculate pricing
        gross_amount = Decimal('0.00')
        validated_items = []
        
        for item in items_data:
            product_id = item.get("product_id")
            quantity = item.get("quantity", 1)
            
            try:
                product = await product_manager.fetch(product_id)
                if not product or not product.is_active:
                    continue
            except:
                continue
                
            item_gross = quantity * product.cost_price
            gross_amount += item_gross
            
            validated_items.append({
                "product": product,
                "quantity": quantity,
                "unit_price": product.cost_price,
                "subtotal": item_gross
            })
            
        if not validated_items:
            print("❌ No valid active products found in CRM order")
            return

        # 3. Get Default Telecaller/Admin for CRM Orders
        # Find the first admin to attribute this order
        admin_users = await user_manager.fetch_all(filters={"role": UserRole.ADMIN, "is_active": True})
        telecaller_id = admin_users.items[0].uid if admin_users.items else None
        
        if not telecaller_id:
            # Fallback to any active user if no admin found
            all_users = await user_manager.fetch_all(filters={"is_active": True})
            telecaller_id = all_users.items[0].uid if all_users.items else None

        if not telecaller_id:
            print("❌ No active user found to attribute CRM order")
            return

        # 4. Create Order Number
        order_number = generate_order_number()
        
        # 5. Create Order Schema
        new_order = CustomerOrderSchema(
            order_number=order_number,
            customer_name=customer_name,
            customer_phone=customer_phone,
            address_line=address_line,
            district=district,
            state=state,
            pincode=pincode,
            telecaller_id=telecaller_id,
            order_status=OrderStatus.PENDING,
            collection_type=CollectionType.DELIVERY,
            payment_method=PaymentMethod.CASH,
            order_date=datetime.utcnow(),
            gross_amount=gross_amount,
            total_amount=gross_amount,
            status_remarks="Order received via CRM Webhook"
        )
        
        created_order = await order_manager.create(new_order)
        
        # 6. Create Order Items
        for item_data in validated_items:
            order_item = OrderItemSchema(
                order_id=created_order.uid,
                product_id=item_data["product"].uid,
                quantity=item_data["quantity"],
                unit_price=item_data["unit_price"],
                total_price=item_data["subtotal"],
                subtotal=item_data["subtotal"]
            )
            await order_item_manager.create(order_item)
            
        # 7. Auto-assign Outlet
        assigned_outlet = await auto_assign_outlet(district, state, payload.get("taluk"))
        if assigned_outlet:
            await order_manager.update(
                created_order.uid,
                {"assigned_outlet_id": assigned_outlet.uid}
            )
            
            # 8. Reserve Stock
            try:
                await reserve_crm_stock(created_order.uid, assigned_outlet.uid, validated_items)
            except Exception as e:
                print(f"⚠️ Stock reservation failed: {str(e)}")
                await order_manager.update(
                    created_order.uid,
                    {"status_remarks": f"Order received via CRM, but stock reservation failed: {str(e)}"}
                )
                
        # 9. Log Activity
        try:
            activity_log = ActivityLogSchema(
                user_id=telecaller_id,
                action="CRM_WEBHOOK_ORDER_CREATED",
                entity_type="customer_orders",
                entity_id=created_order.uid,
                details={
                    "order_number": created_order.order_number,
                    "customer_name": customer_name,
                    "total_amount": float(gross_amount)
                }
            )
            await activity_manager.create(activity_log)
        except:
            pass
            
        print(f"✅ CRM Order {order_number} processed successfully")
        
    except Exception as e:
        print(f"❌ Critical error in CRM background task: {str(e)}")
