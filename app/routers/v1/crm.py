from fastapi import APIRouter, HTTPException, Depends, status, Body, Path, Query, BackgroundTasks
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, date, timedelta
from decimal import Decimal
import uuid
import logging
from pypinindia import get_district, get_pincode_info, get_state

from config import get_settings, get_engine
from managers import (
    CustomerOrderManager, OrderItemManager, OrderTransactionManager,
    InventoryManager, ProductManager, OutletManager, UserManager,
    InventorySchema, CustomerOrderSchema, OrderItemSchema, OrderTransactionSchema,
    ActivityLogManager, ActivityLogSchema, OutletSchema, LSQTelecallerMappingSchema, LSQTelecallerMappingManager ,LSQOrderAdSchema , LSQOrderAdManager
)
from services import CRMService
from utils.outlet_assignment import auto_assign_outlet, push_outlet_not_assigned, push_outlet_assigned
from utils.crm_utils import sync_order_to_crm
from utils.constants import UserRole, OrderStatus, PaymentStatus, CollectionType, PaymentMethod, ActivityType, LSQCreateOrder, LSQItems, HASSAN_OUTLET_ID
from utils.inventory_utils import is_hassan_or_warehouse, sync_unified_inventory
from models import CrmPayload, OrderCreateRequest


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
lsq_telecaller_manager = LSQTelecallerMappingManager(engine)
lsq_order_ad_manager = LSQOrderAdManager(engine)
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
async def test(district: Optional[str] = None, pincode: Optional[str] = None, taluk: Optional[str] = None):
    # Validate that either pincode or (district AND taluk) are provided
    if not (pincode or (district and taluk)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either pincode or both district and taluk must be provided"
        )
    
    # Call auto_assign_outlet with dynamic parameters
    # Note: state is required for the unified function but can be empty string or None
    outlet = await auto_assign_outlet(
        engine,
        district=district,
        pincode=pincode,
        state=None,
        taluk=taluk
    )
    
    if not outlet:
        raise HTTPException(status_code=404, detail="No outlet could be assigned for the given location")

    outlet_dict = outlet.model_dump()
    
    # For response info, try to resolve district if it was missing
    if not district and pincode:
        try:
            resolved_district = get_district(pincode)
            district = resolved_district[0] if isinstance(resolved_district, list) and resolved_district else resolved_district
            taluk = resolved_district[0].get('taluk')
        except:
            pass
            
    outlet_dict["lib_dictrict"] = district
    outlet_dict["lib_taluk"] = taluk
    return outlet_dict
    # outlet = await order_manager.fetch(
    #         pincode,
    #         joins=[
    #             (CustomerOrderSchema.assigned_outlet, OutletSchema.manager),
    #             (CustomerOrderSchema.items, OrderItemSchema.product)
    #         ]
    #     )
    # return outlet.model_dump()
    # await order_creation_success_activity(order_id)

@router.post("/webhook")
async def webhook(background_tasks: BackgroundTasks, payload: dict = Body(None)):
    """Receives webhook from CRM and processes it in background"""
    background_tasks.add_task(process_crm_orders, payload)
    return {"message": "Webhook received, processing in background"}



async def _find_crm_inventory_for_product(product_id: str, outlet_id: str):
    """Find inventory record with unified warehouse-Hassan pool support."""
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


async def reserve_crm_stock(order_id: str, outlet_id: str, validated_items: List[dict]):
    """Reserve stock for order items at assigned outlet (warehouse-Hassan coupled)."""
    for item_data in validated_items:
        product_id = item_data["product"].uid
        quantity = item_data["quantity"]

        inventory_item = await _find_crm_inventory_for_product(product_id, outlet_id)

        if inventory_item is None:
            logging.warning(f"No stock available for product {item_data['product'].product_name}")
            continue

        available = inventory_item.quantity - inventory_item.reserved_quantity
        if available < quantity:
            logging.warning(f"Insufficient stock for {item_data['product'].product_name}. Available: {available}, Required: {quantity}")
            continue

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


def generate_order_number() -> str:
    """Generate unique order number"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"ORD-CRM-{timestamp}-{str(uuid.uuid4())[:8].upper()}"

async def order_creation_success_activity(order_id: str):
    """Push an ORDER_STATUS activity to CRM indicating order was created successfully."""
    await sync_order_to_crm(engine, order_id, ActivityType.ORDER_STATUS)


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
            district = customer_data.get("mx_City", "")
            state = customer_data.get("mx_State", "")
            order_owner = products_data.get(LSQCreateOrder.OWNER.value, "")
            pincode = products_data.get(LSQCreateOrder.PINCODE.value, "")
            taluk = customer_data.get("taluk")
            crm_order_id = products_data.get("mx_Custom_8") # this will come form medusa 
            payment_method_raw = (products_data.get(LSQCreateOrder.PAYMENT_METHOD.value) or "").strip().lower()
            prepaid_amount_raw = products_data.get(LSQCreateOrder.PREPAID_AMOUNT.value)
            
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
            crm_order_id = cleaned_payload.get("mx_Custom_8")
        
        # Deduplication check: If crm_order_id is provided, check if it already exists in ERP
        if crm_order_id:
            try:
                existing_order = await order_manager.fetch(str(crm_order_id))
                if existing_order:
                    print(f"⚠️ CRM order {crm_order_id} already exists in ERP. Skipping duplicate creation.")
                    return
            except Exception as e:
                # If fetch fails (e.g. order not found), continue with creation
                pass
            payment_method_raw = (cleaned_payload.get("payment_method") or "").strip().lower()
            prepaid_amount_raw = cleaned_payload.get("prepaid_amount")
        
        try:
            if pincode:
                # Use pincode as the primary source of truth for location
                res_district = get_district(pincode)
                if res_district:
                    district = res_district[0] if isinstance(res_district, list) and res_district else res_district
                
                res_state = get_state(pincode)
                if res_state:
                    state = res_state
        except Exception as e:
            logging.error(f"Error looking up pincode {pincode}: {e}")
            
        # Default fallbacks if lookups failed or district/state still missing
        if not district:
            district = "Hassan"
        if not state:
            state = "karnataka"

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
                commission_base = calculated_unit_price - product.unit_price
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
            lsq_telecaller_record = await lsq_telecaller_manager.fetch_one(filters={"lsq_id": order_owner}, joins = {LSQTelecallerMappingSchema.telecaller})
            telecaller_record = await user_manager.fetch_one(filters={"uid": lsq_telecaller_record.telecaller.uid, "is_active": True})
            telecaller_id = telecaller_record.uid
        except:
            admin_record = await user_manager.fetch_one(filters={"role": UserRole.ADMIN, "is_active": True})
            telecaller_id = admin_record.uid

        if not telecaller_id:
            print("❌ No active user found to attribute CRM order")
            return

        # Calculate final pricing aligned with orders.py
        discount_applied = product_discount_total
        amount_after_discount = gross_amount - product_discount_total
        
        # If LSQ provides GRAND_TOTAL, default to it, else use formula
        base_total_amount = Decimal(str(order_total)) if order_total else amount_after_discount
        
        # Process prepaid amount
        prepaid_amt_dec = Decimal('0.00')
        if payment_method_raw == "online" and prepaid_amount_raw is not None:
            try:
                prepaid_amt_dec = Decimal(str(prepaid_amount_raw))
            except Exception:
                print(f"⚠️ Could not parse prepaid_amount '{prepaid_amount_raw}', defaulting to 0")
        
        # Calculate final total amount (remaining amount to be paid)
        final_total_amount = max(Decimal('0.00'), base_total_amount - prepaid_amt_dec)

        # 4. Create Order Number
        order_number = generate_order_number()
        
        # 5. Create Order Schema
        order_kwargs = dict(
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
            payment_method=PaymentMethod.ONLINE if payment_method_raw == "online" else PaymentMethod.CASH,
            order_date=datetime.utcnow(),
            gross_amount=gross_amount,
            expected_delivery_date=expected_delivery_date,
            manual_discount=discount_applied,
            discount_applied=discount_applied,
            total_amount=final_total_amount,
            total_commission=total_commission,
            priority_level=10
        )
        
        # For online/prepaid orders, attach the prepaid amount
        if payment_method_raw == "online" and prepaid_amt_dec > 0:
            order_kwargs["prepaid_amount"] = prepaid_amt_dec
        
        if crm_order_id:
            order_kwargs["uid"] = str(crm_order_id)
            
        new_order = CustomerOrderSchema(**order_kwargs)
        # print(new_order.model_dump())
        
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
            print(order_item.model_dump())
            await order_item_manager.create(order_item)
            
        # 7. Create Prepaid Transaction if Online
        if payment_method_raw == "online" and prepaid_amt_dec > 0:
            try:
                transaction = OrderTransactionSchema(
                    order_id=created_order.uid,
                    payment_status=PaymentStatus.PAID,
                    payment_method=PaymentMethod.ONLINE,
                    amount_paid=prepaid_amt_dec,
                    transaction_reference=f"CRM_PREPAID_{created_order.order_number}",
                    payment_date=datetime.utcnow(),
                    received_by=telecaller_id,
                    notes="Prepaid amount recorded from CRM webhook"
                )
                await transaction_manager.create(transaction)
                print(f"✅ Created prepaid transaction for {prepaid_amt_dec}")
                # Sync payment status activity to CRM
                await sync_order_to_crm(engine, created_order.uid, ActivityType.PAYMENT_STATUS)
            except Exception as txn_err:
                print(f"⚠️ Failed to create prepaid transaction: {str(txn_err)}")

                    
            # lsq ad data on the lead object
        lsq_order_ad_data = LSQOrderAdSchema(
            lead_id=customer_data.get("ProspectID"),
            order_id=created_order.uid,
            ad_id=customer_data.get("mx_Ad_Id"),
            ad_set_id=customer_data.get("mx_Ad_Set_Id"),
            campaign_id=customer_data.get("mx_Campaign_Id"),
            lead_stage=customer_data.get("ProspectStage"),
            lead_source=customer_data.get("Source"),
            source_campaign=customer_data.get("SourceCampaign")
        )
        try:
            lsq_order_ad = await lsq_order_ad_manager.create(lsq_order_ad_data)
            print(lsq_order_ad.model_dump())
        except Exception as lsq_order_ad_err:
            print(lsq_order_ad_data.model_dump())
            print(f"⚠️ Failed to create LSQ order ad data: {str(lsq_order_ad_err)}")


        # 8. Auto-assign Outlet
        assigned_outlet = await auto_assign_outlet(
            engine,
            created_order.uid,
            district,
            pincode,
            state,
            taluk
        )
        if assigned_outlet:
            # print(assigned_outlet.uid)
            await order_manager.update(
                created_order.uid,
                {"assigned_outlet_id": assigned_outlet.uid}
            )
            
            # 9. Reserve Stock
            try:
                await reserve_crm_stock(created_order.uid, assigned_outlet.uid, validated_items)
            except Exception as e:
                print(f"⚠️ Stock reservation failed: {str(e)}")
                await order_manager.update(
                    created_order.uid,
                    {"status_remarks": f"Order received via CRM, but stock reservation failed: {str(e)}"}
                )
                
        # 10. Push order-confirmed (ORDER_STATUS) activity to CRM
        # await push_outlet_assigned(engine, created_order.uid, district, pincode)
        # await order_creation_success_activity(created_order.uid)
            
        # 11. Log Activity
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
