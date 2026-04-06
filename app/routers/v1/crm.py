from fastapi import APIRouter, HTTPException, Depends, status, Body, Path, Query, BackgroundTasks
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, date, timedelta
from decimal import Decimal
import uuid
import logging
import httpx
from pypinindia import get_district,get_pincode_info ,get_state

from config import get_settings, get_engine
from managers import (
    CustomerOrderManager, OrderItemManager, OrderTransactionManager,
    InventoryManager, ProductManager, OutletManager, UserManager,
    CustomerOrderSchema, OrderItemSchema, OrderTransactionSchema,
    ActivityLogManager, ActivityLogSchema
)
from services import CRMService
from utils.constants import UserRole, OrderStatus, PaymentStatus, CollectionType, PaymentMethod , ActivityType, LSQCreateOrder , LSQItems
from models import CrmPayload , OrderCreateRequest


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

crm_service = CRMService()

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


@router.post("/test")
async def test(payload:dict):
    return crm_service.clean_lsq_payload(payload)

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


async def auto_assign_outlet(district: str, pincode: str, taluk: str = None) -> Optional[object]:
    """Auto-assign outlet based on Google Sheets mapping"""
    try:
        # Google Apps Script endpoint for outlet mapping
        GOOGLE_OUTLET_API = "https://script.google.com/macros/s/AKfycbxqlS7Og-4AKNm79aweOzjVqOcmyFXHaHpy7xmfcz27i0knowG_vjEFWZ_Ha8drY4CYbA/exec"
        
        def get_taluk_name(pincode):
            data = get_pincode_info(pincode)
            if data:
                # returns the Taluk of the first post office found
                return data[0].get('taluk') 
            return "Not Found"

        # Prepare API request parameters
        taluk = get_taluk_name(pincode)
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
            
            matched_outlet = None
            for outlet in outlets.items:
                outlet_first_word = outlet.outlet_name.lower().split()[0] if outlet.outlet_name else ""
                
                if api_first_word == outlet_first_word:
                    matched_outlet = outlet
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

        cleaned_payload = crm_service.clean_lsq_payload(payload)
        # print(cleaned_payload)
        # Handle both LeadSquared webhook payload and standard test payload
        if "Current" in cleaned_payload and "Data" in cleaned_payload:
            products_data = cleaned_payload.get("Data")
            customer_data = cleaned_payload.get("Current")

            print(f"products_data: {products_data}")
            print(f"customer_data: {customer_data}")
            
            customer_name = f'{customer_data.get("FirstName", "")} {customer_data.get("LastName", "")}'.strip()
            customer_phone = customer_data.get("Phone")
            address_line = customer_data.get("mx_Street1", "")
            # district = customer_data.get("mx_City", "")
            # state = customer_data.get("mx_State", "")
            order_owner = products_data.get(LSQCreateOrder.OWNER.value, "")
            pincode = product_data.get(LSQCreateOrder.PINCODE.value, "")
            taluk = customer_data.get("taluk")
            
            no_of_items = int(products_data.get(LSQCreateOrder.NO_OF_ITEMS.value, 0) or 0)
            order_total = products_data.get(LSQCreateOrder.GRAND_TOTAL.value, 0)
            collection_type = products_data.get(LSQCreateOrder.COLLECTION_TYPE.value)
            items_data = []
            item_keys = [
                LSQCreateOrder.ITEM_1.value, 
                LSQCreateOrder.ITEM_2.value, 
                LSQCreateOrder.ITEM_3.value
            ]
            
            for i in range(no_of_items):
                if i >= len(item_keys):
                    break
                raw_item = products_data.get(item_keys[i])
                if raw_item and isinstance(raw_item, dict):
                    product_uid = raw_item.get(LSQItems.PRODUCT_ID.value, "")
                    sku_code = raw_item.get(LSQItems.SKU_CODE.value, "")
                    quantity = int(raw_item.get(LSQItems.QUANTITY.value, 1) or 1)
                    
                    if product_uid or sku_code:
                        items_data.append({
                            "product_id": product_uid,
                            "sku_code": sku_code,
                            "quantity": quantity,
                            "product_name": raw_item.get(LSQItems.PRODUCT_NAME.value),
                            "category": raw_item.get(LSQItems.CATEGORY.value),
                            "brand_name": raw_item.get(LSQItems.BRAND_NAME.value),
                            "unit_type": raw_item.get(LSQItems.UNIT_TYPE.value),
                            "size": raw_item.get(LSQItems.SIZE.value),
                            "mrp": raw_item.get(LSQItems.MRP.value),
                            "selling_price": raw_item.get(LSQItems.SELLING_PRICE.value),
                            "total_price": raw_item.get(LSQItems.TOTAL_PRICE.value),
                            "subtotal": raw_item.get(LSQItems.TOTAL_PRICE.value),
                            "discount_amount_per_unit": raw_item.get(LSQItems.DISCOUNT_AMOUNT_PER_UNIT.value),
                            "product_description": raw_item.get(LSQItems.PRODUCT_DESCRIPTION.value)
                        })
        else:
            customer_name = cleaned_payload.get("customer_name")
            customer_phone = cleaned_payload.get("customer_phone")
            address_line = cleaned_payload.get("address_line", "")
            district = cleaned_payload.get("district", "")
            state = cleaned_payload.get("state", "")
            pincode = cleaned_payload.get("pincode", "560001")
            taluk = cleaned_payload.get("taluk")
            items_data = cleaned_payload.get("items", [])
        
        if district == "":
            district = get_district(pincode)
        if state == "":
            state = get_state(pincode)

        # Calculate expected delivery date (7 business days)
        days_added = 0
        expected_delivery_date = datetime.utcnow()
        while days_added < 7:
            expected_delivery_date += timedelta(days=1)
            if expected_delivery_date.weekday() < 5:  # 0-4 are Monday to Friday
                days_added += 1
        
        if not all([customer_name, customer_phone, pincode, items_data]):
            print("Missing mandatory fields in CRM payload")
            return

        # 2. Resolve items and calculate pricing
        gross_amount = Decimal('0.00')
        product_discount_total = Decimal('0.00')
        total_commission = Decimal('0.00')
        validated_items = []
        
        print(f"items_data: {items_data}")
        for item in items_data:
            product_id = item.get("product_id")
            quantity = int(item.get("quantity", 1) or 1)
            sku = item.get("sku_code")
            
            try:
                product = await product_manager.fetch_one(filters={"sku": sku})
                if not product or not product.is_active:
                    continue
            except:
                continue
                
            discount_per_unit = Decimal(item.get("discount_amount_per_unit") or '0.00')
            lsq_total_price = item.get("total_price") or item.get("subtotal")
            
            if lsq_total_price:
                subtotal = Decimal(str(lsq_total_price))
                calculated_unit_price = subtotal / Decimal(str(quantity)) if quantity > 0 else Decimal('0.00')
            else:
                calculated_unit_price = max(Decimal('0.00'), product.cost_price - discount_per_unit)
                subtotal = Decimal(str(quantity)) * calculated_unit_price
                
            item_gross = Decimal(str(quantity)) * product.cost_price
            gross_amount += item_gross
            
            product_manual_discount = discount_per_unit * Decimal(str(quantity))
            product_discount_total += product_manual_discount
            
            # Commission calculation logic
            if product.margin > 0:
                commission_base = subtotal - product.unit_price
            else:
                commission_base = max(product.cost_price - discount_per_unit, Decimal('0.00'))
                
            commission_per_unit = commission_base * (product.commission / Decimal('100.00'))
            item_commission = commission_per_unit * Decimal(str(quantity))
            total_commission += item_commission
            
            validated_items.append({
                "product": product,
                "quantity": quantity,
                "unit_price": calculated_unit_price,
                "subtotal": subtotal,
                "gross_amount": item_gross,
                "product_manual_discount": product_manual_discount,
                "commission": item_commission
            })
        print(f"Validated items: {validated_items}")
        if not validated_items:
            print("❌ No valid active products found in CRM order")
            return

        # 3. Get Default Telecaller/ Admin for CRM Orders
        # Find the first admin to attribute this order
        try:
            telecaller_id = await user_manager.fetch_one(filters={"uid": order_owner, "is_active": True})
        except:
            telecaller_id = None

        if not telecaller_id:
            print("❌ No active user found to attribute CRM order")
            return

        # Calculate final pricing aligned with orders.py
        discount_applied = product_discount_total
        amount_after_discount = gross_amount - product_discount_total
        
        # If LSQ provides GRAND_TOTAL, default to it, else use formula
        final_total_amount = Decimal(str(order_total)) if order_total else amount_after_discount

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
            collection_type=collection_type if collection_type else CollectionType.DOORSTEP,
            payment_method=PaymentMethod.CASH,
            order_date=datetime.utcnow(),
            gross_amount=gross_amount,
            manual_discount=discount_applied,
            discount_applied=discount_applied,
            total_amount=final_total_amount,
            total_commission=total_commission
        )
        # print(new_order)
        
        created_order = await order_manager.create(new_order)
        
        # 6. Create Order Items
        for item_data in validated_items:
            order_item = OrderItemSchema(
                order_id=created_order.uid,
                product_id=item_data["product"].uid,
                quantity=item_data["quantity"],
                unit_price=item_data["unit_price"],
                total_price=item_data["subtotal"],
                subtotal=item_data["subtotal"],
                product_manual_discount=item_data["product_manual_discount"]
            )
            print(order_item)
            await order_item_manager.create(order_item)
            
        # 7. Auto-assign Outlet
        assigned_outlet = await auto_assign_outlet(district,pincode, customer_data.get("taluk"))
        if assigned_outlet:
            # print(assigned_outlet.uid)
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
                    "total_amount": float(gross_amount),
                    "status_remarks": "order received via CRM"
                }
            )
            await activity_manager.create(activity_log)
        except Exception:
            print(f"❌ Activity log creation failed")
            
        print(f"✅ CRM Order {order_number} processed successfully")
        
    except Exception as e:
        print(f"❌ Critical error in CRM background task: {str(e)}")
