import httpx
import asyncio
from typing import Dict, Any, Optional
from config import get_settings
from utils.constants import LeadSource , LSQOrderStatusActivityField ,LSQProductField , ActivityType ,LSQDeliveryStatusActivityField , LSQPaymentStatusActivityField , LSQRefundStatusActivityField ,LSQCreateOrder , LSQItems
from models import CrmPayload , OrderCreateRequest
from pydantic import BaseModel
import json

class Activity:
    class orders_status:
        CONFIRMED = "Confirmed"
        COMPLETED = "Completed"
        CANCELLED = "Cancelled"
    class delivery_status:
        NOT_ASSIGNED = "Not Assigned"
        ASSIGNED = "Assigned"
        DELIVERED = "Delivered"
        OUT_FOR_DELIVERY = "Out For Delivery"
        CANCELLED = "Cancelled"
    class payment_status:
        PENDING = "Pending"
        PAID = "Paid"
        PARTIALLY_PAID = "Partially Paid"
        FAILED = "Failed"
    class refund_status:
        PROCESSING = "Processing"
        APPROVED = "Approved"
        REJECTED = "Rejected"


class CRMService:
    # -------------------------------------------------------------------------
    # MAPPING CONFIGURATION
    # -------------------------------------------------------------------------
    # Base mapping (Readable Key -> LeadSquared Key)
    LEADSQUARED_PAYMENT_STATUS_ACTIVITY_MAPPING = {
        "activity_note":        LSQPaymentStatusActivityField.ACTIVITY_EVENT_NOTE,
        "payment_status":       LSQPaymentStatusActivityField.PAYMENT_STATUS,
        "order_id":             LSQPaymentStatusActivityField.ORDER_ID,
        "amount_paid":          LSQPaymentStatusActivityField.AMOUNT_PAID,
        "transaction_reference": LSQPaymentStatusActivityField.TRANSACTION_REFERENCE,
        "payment_date":         LSQPaymentStatusActivityField.PAYMENT_DATE,
        "amount_payable":       LSQPaymentStatusActivityField.AMOUNT_PAYABLE,
        "failure_reason":       LSQPaymentStatusActivityField.FAILURE_REASON,
        "failed_at":            LSQPaymentStatusActivityField.FAILED_AT,
    }

    LEADSQUARED_DELIVERY_STATUS_ACTIVITY_MAPPING = {
        "activity_note":          LSQDeliveryStatusActivityField.ACTIVITY_EVENT_NOTE,
        "order_status":           (LSQDeliveryStatusActivityField.STATUS, LSQDeliveryStatusActivityField.ORDER_STATUS),
        "order_number":               LSQDeliveryStatusActivityField.ORDER_ID,
        "assigned_outlet.outlet_name":            LSQDeliveryStatusActivityField.OUTLET_NAME,
        "assigned_outlet.address":        LSQDeliveryStatusActivityField.OUTLET_LOCATION,
        "assigned_outlet.phone":           LSQDeliveryStatusActivityField.OUTLET_PHONE,
        "assigned_outlet.manager.full_name":    (LSQDeliveryStatusActivityField.OUTLET_MANAGER_NAME, LSQDeliveryStatusActivityField.DELIVERY_AGENT_NAME),
        "assigned_outlet.manager.phone":   (LSQDeliveryStatusActivityField.OUTLET_MANAGER_PHONE, LSQDeliveryStatusActivityField.DELIVERY_AGENT_PHONE),
        "expected_delivery_date": LSQDeliveryStatusActivityField.EXPECTED_DELIVERY_DATE,
        "delivery_remarks":       LSQDeliveryStatusActivityField.DELIVERY_REMARKS,
        "assigned_at":            LSQDeliveryStatusActivityField.ASSIGNED_AT,
        "actual_delivery_date":           LSQDeliveryStatusActivityField.DELIVERED_AT,
        "status_remarks":         LSQDeliveryStatusActivityField.DELIVERY_NOTES,
        "transaction_reference":  LSQDeliveryStatusActivityField.TRANSACTION_REFERENCE,
        "payment_status":         LSQDeliveryStatusActivityField.PAYMENT_STATUS,
        "created_at":             LSQDeliveryStatusActivityField.CREATED_AT,
        "assignment_pending_reason":     LSQDeliveryStatusActivityField.ASSIGNMENT_PENDING_REASON,
        "return_type":            LSQDeliveryStatusActivityField.RETURN_TYPE,
        "return_reason":          LSQDeliveryStatusActivityField.RETURN_REASON,
        "out_for_delivery_at":    LSQDeliveryStatusActivityField.OUT_FOR_DELIVERY_AT,
        "returned_at":            LSQDeliveryStatusActivityField.RETURNED_AT,
        "refund_status":          LSQDeliveryStatusActivityField.REFUND_STATUS,
        "refund_amount":          LSQDeliveryStatusActivityField.REFUND_AMOUNT,
    }

    LEADSQUARED_ORDER_STATUS_ACTIVITY_MAPPING = {
        "order_status":         LSQOrderStatusActivityField.ORDER_STATUS,
        "user_id":              LSQOrderStatusActivityField.USER_ID,
        "order_id":             LSQOrderStatusActivityField.ORDER_ID,
        "customer_name":        LSQOrderStatusActivityField.CUSTOMER_NAME,
        "customer_phone":       LSQOrderStatusActivityField.CUSTOMER_PHONE,
        "customer_email":       LSQOrderStatusActivityField.CUSTOMER_EMAIL,
        "address_line":         LSQOrderStatusActivityField.ADDRESS_LINE,
        "city":                 LSQOrderStatusActivityField.CITY,
        "state":                LSQOrderStatusActivityField.STATE,
        "pincode":              LSQOrderStatusActivityField.PINCODE,
        "total_amount":         LSQOrderStatusActivityField.TOTAL_AMOUNT,
        "discount_applied":     LSQOrderStatusActivityField.TOTAL_DISCOUNT,
        "payment_method":       LSQOrderStatusActivityField.PAYMENT_METHOD,
        "created_at":           LSQOrderStatusActivityField.CREATED_AT,
        "invoice":              LSQOrderStatusActivityField.INVOICE,
        "coupon_code_status":   LSQOrderStatusActivityField.COUPON_CODE_STATUS,
        "delivery_status":      LSQOrderStatusActivityField.DELIVERY_STATUS,
        "payment_status":       LSQOrderStatusActivityField.PAYMENT_STATUS,
        "items":                LSQOrderStatusActivityField.ITEMS, 
        "refund_status":        LSQOrderStatusActivityField.REFUND_STATUS,
        "actual_delivery_date": LSQOrderStatusActivityField.ACTUAL_DELIVERY_DATE,
        "coupon_code":          LSQOrderStatusActivityField.COUPON_CODE,
        "status_remarks":       LSQOrderStatusActivityField.REASON,
    }

    LEADSQUARED_PRODUCT_MAPPING = {
        "product_name":         LSQProductField.PRODUCT_NAME,
        "product_title":        LSQProductField.PRODUCT_TITLE,
        "quantity":             LSQProductField.QUANTITY,
        "size":                 LSQProductField.SIZE,
        "unit_type":            LSQProductField.UNIT_TYPE,
        "mrp":                  LSQProductField.MRP,
        "selling_price":        LSQProductField.SELLING_PRICE,
        "discount":             LSQProductField.DISCOUNT,
        "total_price":          LSQProductField.TOTAL_PRICE,
    }
    LEADSQUARED_ACTIVITY_CODE_MAPPING = {
        "order_status": 203,
        "payment_status": 205,
        "refund_status": 204,
        "delivery_status": 206
    }
    LSQCreateOrder={
        "status_remarks"  : LSQCreateOrder.NOTES,
        "order_status"    : LSQCreateOrder.STATUS,
        "owner"           : LSQCreateOrder.OWNER,
        "item_1"          : LSQCreateOrder.ITEM_1,
        "item_2"          : LSQCreateOrder.ITEM_2,
        "item_3"          : LSQCreateOrder.ITEM_3,
        "no_of_items"     : LSQCreateOrder.NO_OF_ITEMS,
        "grand_total"     : LSQCreateOrder.GRAND_TOTAL,
        "collection_type" : LSQCreateOrder.COLLECTION_TYPE,
        "order_id"        : LSQCreateOrder.ORDER_ID,
        "pincode"         : LSQCreateOrder.PINCODE
    }

    LSQItems={
        "discount_amount_per_unit" : LSQItems.DISCOUNT_AMOUNT_PER_UNIT,
        "product_name"             : LSQItems.PRODUCT_NAME,
        "category"                 : LSQItems.CATEGORY,
        "brand_name"               : LSQItems.BRAND_NAME,
        "sku_code"                 : LSQItems.SKU_CODE,
        "unit_type"                : LSQItems.UNIT_TYPE,
        "size"                     : LSQItems.SIZE,
        "mrp"                      : LSQItems.MRP,
        "selling_price"            : LSQItems.SELLING_PRICE,
        "quantity"                 : LSQItems.QUANTITY,
        "total_price"              : LSQItems.TOTAL_PRICE,
        "product_id"               : LSQItems.PRODUCT_ID,
        "product_description"      : LSQItems.PRODUCT_DESCRIPTION
    }
    # SchemaName for the product custom-object container block
    PRODUCT_OBJECT_SCHEMA = LSQOrderStatusActivityField.ITEMS

    def __init__(self):
        self.settings = get_settings()
        self.access_key = self.settings.crm_api_key
        self.secret_key = self.settings.crm_secret_key
        self.base_url = self.settings.crm_url
        self._timeout = 10.0

    async def _execute_with_retry(self, method: str, url: str, params: dict, **kwargs) -> None:
        """The background worker that handles the retries."""
        retriable_statuses = {408, 425, 429, 500, 502, 503, 504}
        backoff_schedule = [30, 120, 420, 900]
        
        attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(method, url, params=params, **kwargs)

                if 200 <= response.status_code < 300:
                    print(f"Background Task Success: {url} | Status: {response.status_code}")
                    return 

                if response.status_code in retriable_statuses and attempt < len(backoff_schedule):
                    await asyncio.sleep(backoff_schedule[attempt])
                    attempt += 1
                    continue

                print(f"Background Task Failed: {response.status_code} | Msg: {response}")
                break

            except httpx.RequestError as e:
                if attempt < len(backoff_schedule):
                    await asyncio.sleep(backoff_schedule[attempt])
                    attempt += 1
                    continue
                print(f"Background Task Request Error: {e}")
                break

    async def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, str]:
        """Triggers the request in the background and returns immediately (Fire and forget)."""
        url = f"{self.base_url}{endpoint}"
        params = kwargs.pop("params", {}) or {}
        params["accessKey"] = self.access_key
        params["secretKey"] = self.secret_key

        asyncio.create_task(self._execute_with_retry(method, url, params, **kwargs))
        return {"status": "queued", "message": "Request is being processed in the background."}

    async def _await_request(self, method: str, endpoint: str, **kwargs) -> Any:
        """Triggers a request and waits for the JSON response (Blocking async)."""
        url = f"{self.base_url}{endpoint}"
        params = kwargs.pop("params", {}) or {}
        params["accessKey"] = self.access_key
        params["secretKey"] = self.secret_key

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.request(method, url, params=params, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                print(f"CRM HTTP Error: {e.response.status_code} - {e.response.text}")
                return {"error": True, "details": e.response.json() if e.response.text else "HTTP Error"}
            except Exception as e:
                print(f"CRM Request Error: {e}")
                return {"error": True, "details": str(e)}
    def _build_custom_object_array(self, item_dict: dict, object_mapping: dict) -> list:
        """
        Converts a standard dictionary into a LeadSquared Custom Object Fields array.
        """
        custom_fields = []
        
        for item_key, schema_name in object_mapping.items():
            value = item_dict.get(item_key)
            
            if value is not None and value != "":
                # Use .value to extract the raw string from str-Enums
                schema_str = schema_name.value if hasattr(schema_name, "value") else str(schema_name)
                value_str = value.value if hasattr(value, "value") else str(value)
                custom_fields.append({
                    "SchemaName": schema_str,
                    "Value": value_str
                })
                
        return custom_fields
    def _get_nested_value(self, data: dict, path: str):
        """Helper to get value from nested dictionary using dot notation."""
        for key in path.split('.'):
            if isinstance(data, dict):
                data = data.get(key)
            else:
                return None
        return data

    async def build_payload(self, payload_dict: dict, lead_id: str, activity_event_code: int) -> dict:
        fields = []
        # print("--"*50)
        # print(payload_dict)
        # print("--"*50)

        mapping = {}
        if activity_event_code == 203:
            mapping = self.LEADSQUARED_ORDER_STATUS_ACTIVITY_MAPPING
        elif activity_event_code == 205:
            mapping = self.LEADSQUARED_PAYMENT_STATUS_ACTIVITY_MAPPING
        elif activity_event_code == 206:
            mapping = self.LEADSQUARED_DELIVERY_STATUS_ACTIVITY_MAPPING

        for payload_key, schema_names in mapping.items():
            value = self._get_nested_value(payload_dict, payload_key)
            
            # Normalize to resolve single or multi-field mappings
            if not isinstance(schema_names, (list, tuple)):
                schema_names = [schema_names]
                
            for schema_name in schema_names:
                # Always extract the raw string from str-Enum SchemaName
                schema_str = schema_name.value if hasattr(schema_name, "value") else str(schema_name)
                
                # Handle the specific list comprehension for 'items'
                if payload_key == "items" and isinstance(value, list) and value:
                    for raw_item in value:
                        # Pull the nested product sub-dict if the join was loaded
                        product_info = raw_item.get("product") or {}
                        if hasattr(product_info, "__dict__"):
                            product_info = product_info.__dict__

                    # Build a flat dict that matches LEADSQUARED_PRODUCT_MAPPING keys:
                    #   product mapping key  ←  source field
                    #   product_name         ←  product.product_name  (product join)
                    #   product_title        ←  product.product_name  (same; no separate title field)
                    #   quantity             ←  item.quantity
                    #   size                 ←  (no size field on OrderItem; leave blank)
                    #   unit_type            ←  product.unit_of_measure
                    #   mrp                  ←  product.unit_price  (catalogue price)
                    #   selling_price        ←  item.unit_price     (actual charged price)
                    #   discount             ←  item.discount_amount
                    #   total_price          ←  item.total_price
                        enriched_item = {
                            "product_name":  product_info.get("product_name"),
                            "product_title": product_info.get("product_name"),
                            "quantity":      raw_item.get("quantity"),
                            "size":          None, 
                            "unit_type":     product_info.get("unit_of_measure"),
                            "mrp":           product_info.get("cost_price") or raw_item.get("cost_price"),
                            "selling_price": raw_item.get("subtotal"),
                            "discount":      raw_item.get("discount_amount"),
                            "total_price":   raw_item.get("total_price"),
                        }

                        inner_fields = self._build_custom_object_array(
                            enriched_item,
                            self.LEADSQUARED_PRODUCT_MAPPING
                        )

                        if inner_fields:
                            fields.append({
                                "SchemaName": self.PRODUCT_OBJECT_SCHEMA.value,
                                "Value": "",
                                "Fields": inner_fields
                            })
                        
                # Handle numerical values that need string conversion
                elif payload_key in ["total_amount", "discount_applied"]:
                    val_str = str(value) if value is not None else "0"
                    fields.append({"SchemaName": schema_str, "Value": val_str})
                    
                # Handle standard fields
                else:
                    if value is not None and value != "":
                        # Use .value to extract raw string from str-Enums (e.g. OrderStatus, PaymentMethod)
                        value_str = value.value if hasattr(value, "value") else str(value)
                        fields.append({"SchemaName": schema_str, "Value": value_str})

        # Extract the string value from Enum if present
        order_status = payload_dict.get('order_status', 'Unknown Status')
        status_str = order_status.value if hasattr(order_status, "value") else str(order_status)

        return {
            "RelatedProspectId": lead_id,
            "ActivityEvent": activity_event_code,
            "ActivityNote": f"{payload_dict.get('order_number', 'Unknown Order')} is {status_str}",
            "Fields": fields
        }

    async def push_activity(self, payload: dict) -> dict:
        """
        1. get or create lead
        2. push activity

        example payload:
        {
            "order_data": {
                "order_number": "123456789",
                "customer_name": "John Doe",
                "customer_phone": "1234567890",
                "customer_email": "[EMAIL_ADDRESS]",
                "address_line": "123 Main St",
                "city": "New York",
                "state": "NY",
                "pincode": "123456",
                "total_amount": "100",
                "total_discount": "10",
                "payment_method": "Credit Card",
                "created_at": "2022-01-01T12:00:00",
                "invoice": "123456789",
                "coupon_code_status": "Applied",
                "delivery_status": "Pending",
                "payment_status": "Pending",
                "items": [
                    {
                        "product_name": "Product 1",
                        "product_title": "Product 1",
                        "quantity": "1",
                        "size": "M",
                        "unit_type": "Piece",
                        "mrp": "100",
                        "selling_price": "100",
                        "discount": "10",
                        "total_price": "100"
                    }
                ],
                "refund_status": "Pending",
                "actual_delivery_date": "2022-01-01T12:00:00",
                "coupon_code": "123456789"
            },
            "activity_event": "order_status"
        }
        """
        activity_data_dict = (
            payload.get("activity_data") or 
            payload.get("order_data") or 
            payload.get("payment_data") or 
            payload.get("delivery_data") or 
            payload.get("refund_data") or
            payload.get("data")
        )
        if not activity_data_dict:
            return {"status": "failed", "message": "Activity data is missing from payload."}

        activity_event = payload.get("activity_event")
        if not activity_event:
            return {"status": "failed", "message": "activity_event is missing from payload."}

        lead_res = await self.get_or_create_lead(activity_data_dict)
        lead_id = lead_res.get("lead_id") if lead_res.get("status") == "success" else None
        if not lead_id:
            return {"status": "failed", "message": "Lead ID (RelatedProspectId) is required."}

        event_string = activity_event.value if hasattr(activity_event, "value") else str(activity_event)
        activity_event_code = self.LEADSQUARED_ACTIVITY_CODE_MAPPING.get(event_string)       
        if not activity_event_code:
             return {"status": "failed", "message": f"Invalid activity type: {event_string}"}

        lsq_payload = await self.build_payload(activity_data_dict, lead_id, activity_event_code)
        # return lsq_payload
        endpoint = "ProspectActivity.svc/Create"
        return await self._await_request("POST", endpoint, json=lsq_payload)
    
    async def get_lead_by_phone(self, phone: str) -> dict:
        """
        1. Checks if a Lead exists by phone number.
        2. Returns the ProspectID if found.
        """
        get_endpoint = "LeadManagement.svc/RetrieveLeadByPhoneNumber"
        get_params = {"phone": phone}
        get_response = await self._await_request("GET", get_endpoint, params=get_params)
        return get_response

    async def create_lead(self, payload: dict) -> dict:
        """
        1. Creates a new Lead.
        2. Returns the ProspectID if found.
        """
        try:
            # 1. Validate the dictionary using your Pydantic model!
            # It will strip out fields that don't belong and validate the types.
            validated_lead = CrmPayload.Lead(**payload)
        except Exception as e:
            print(f"Validation failed for Lead payload: {e}")
            return {"status": "failed", "message": "Invalid lead data format."}

        # 2. Convert it back to a dictionary so we can safely use .get()
        valid_payload = validated_lead.model_dump(exclude_none=True)
        create_endpoint = "LeadManagement.svc/Lead.Capture"
        
        # Base required data
        create_data = [
            {"Attribute": "Phone", "Value": valid_payload.get("customer_phone")},
            {"Attribute": "FirstName", "Value": valid_payload.get("customer_name", "Unknown Customer")},
            {"Attribute": "SearchBy", "Value": "Phone"},
        ]
        
        # Dynamically map remaining optional fields
        Lead_attribute_mapping = {
            "customer_email":   "EmailAddress",
            "Source":           "Source",
            "LastName":         "LastName",
            "Created_On":       "CreatedOn",
            "State":            "mx_State",
            "Country":          "mx_Country",
            "address_line_1":   "mx_Street1",
            "address_line_2":   "mx_Street2",
            "city":             "mx_City",

        }
        
        for payload_key, attr_name in Lead_attribute_mapping.items():
            value = valid_payload.get(payload_key)
            if value:
                create_data.append({"Attribute": attr_name, "Value": value})

        create_params = {"LeadUpdateBehavior": "DoNotUpdate"}
        
        create_response = await self._await_request(
            "POST", 
            create_endpoint, 
            json=create_data, 
            params=create_params 
        )

        # LeadSquared Success Response parsing
        if isinstance(create_response, dict) and create_response.get("Status") == "Success":
            message_obj = create_response.get("Message", {})
            lead_id = message_obj.get("RelatedId") if isinstance(message_obj, dict) else None
            
            if not lead_id and isinstance(message_obj, str):
                lead_id = message_obj 
                
            return {"status": "success", "lead_id": lead_id, "action": "created"}
        else:
            return {"status": "failed", "message": "Failed to create lead", "details": create_response}

    async def get_or_create_lead(self, payload: dict) -> dict:
        """
        1. Checks if a Lead exists by phone number.
        2. Returns the ProspectID if found.
        3. Creates a new Lead if not found and returns the new ProspectID.
        """
        phone = payload.get("customer_phone")
        if not phone:
            return {"status": "failed", "message": "customer_phone is required to find or create a lead."}

        # Step 1: GET Lead by Phone Number
        get_response = await self.get_lead_by_phone(phone)
        
        # If it's a list and has data, the lead exists (simplified condition)
        if isinstance(get_response, list) and get_response:
            lead_id = get_response[0].get("ProspectID")
            return {"status": "success", "lead_id": lead_id, "action": "retrieved"}

        # Step 2: POST Create Lead if it doesn't exist
        create_response = await self.create_lead(payload)
        if create_response.get("status") == "success":
            return create_response

        return {"status": "failed", "message": "Failed to retrieve or create lead.", "crm_response": create_response}

    async def push_to_crm(self, payload: dict) -> dict:
        pass 

    async def pull_from_crm(self) -> dict:
        pass

    def clean_lsq_payload(self,data):
        if isinstance(data, dict):
            cleaned_dict = {}
            for k, v in data.items():
                if v is None or v == "":
                    continue
                if isinstance(v, str) and v.startswith('{"'):
                    try:
                        v = self.clean_lsq_payload(json.loads(v))
                    except json.JSONDecodeError:
                        pass
                elif isinstance(v, dict):
                    v = self.clean_lsq_payload(v)
                cleaned_dict[k] = v
            return cleaned_dict
        elif isinstance(data, list):
            return [self.clean_lsq_payload(item) for item in data if item is not None]
        return data