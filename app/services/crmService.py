import httpx
import asyncio
from typing import Dict, Any, Optional
from config import get_settings
from utils.constants import LeadSource , LSQActivityField ,LSQProductField
from models import CrmPayload , OrderCreateRequest
from pydantic import BaseModel

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

class Push_Activity:
    order_data: OrderCreateRequest
    activity_event_code: str


class CRMService:
    # -------------------------------------------------------------------------
    # MAPPING CONFIGURATION
    # -------------------------------------------------------------------------
    # Base mapping (Readable Key -> LeadSquared Key)
    LEADSQUARED_ACTIVITY_MAPPING = {
        "order_status":         LSQActivityField.ORDER_STATUS,
        "user_id":              LSQActivityField.USER_ID,
        "order_id":             LSQActivityField.ORDER_ID,
        "customer_name":        LSQActivityField.CUSTOMER_NAME,
        "customer_phone":       LSQActivityField.CUSTOMER_PHONE,
        "customer_email":       LSQActivityField.CUSTOMER_EMAIL,
        "address_line":         LSQActivityField.ADDRESS_LINE,
        "city":                 LSQActivityField.CITY,
        "state":                LSQActivityField.STATE,
        "pincode":              LSQActivityField.PINCODE,
        "total_amount":         LSQActivityField.TOTAL_AMOUNT,
        "total_discount":       LSQActivityField.TOTAL_DISCOUNT,
        "payment_method":       LSQActivityField.PAYMENT_METHOD,
        "created_at":           LSQActivityField.CREATED_AT,
        "invoice":              LSQActivityField.INVOICE,
        "coupon_code_status":   LSQActivityField.COUPON_CODE_STATUS,
        "delivery_status":      LSQActivityField.DELIVERY_STATUS,
        "payment_status":       LSQActivityField.PAYMENT_STATUS,
        "items":                LSQActivityField.ITEMS, 
        "refund_status":        LSQActivityField.REFUND_STATUS,
        "actual_delivery_date": LSQActivityField.ACTUAL_DELIVERY_DATE,
        "coupon_code":          LSQActivityField.COUPON_CODE,
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
        "payment status":205,
        "Refund_Status":204,
        "Delivery Status":206
    }

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

                print(f"Background Task Failed: {response.status_code} | Msg: {response.text}")
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
                custom_fields.append({
                    "SchemaName": schema_name,
                    "Value": str(value)
                })
                
        return custom_fields

    async def build_payload(self, order_payload_dict: dict, lead_id: str, activity_event_code: int) -> dict:
        fields = []
        
        for payload_key, schema_name in self.LEADSQUARED_ACTIVITY_MAPPING.items():
            value = order_payload_dict.get(payload_key)
            
            # Handle the specific list comprehension for 'items'
            if payload_key == "items" and isinstance(value, list) and value:
                # Grab the first item (or loop if LeadSquared supports arrays of Custom Objects)
                first_item = value[0] 
                
                inner_fields = self._build_custom_object_array(
                    first_item, 
                    self.LEADSQUARED_PRODUCT_MAPPING
                )
                
                if inner_fields:
                    fields.append({
                        "SchemaName": "mx_Custom_18",  # e.g., "mx_Custom_18"
                        "Value": "",                
                        "Fields": inner_fields
                    })
                    
            # Handle numerical values that need string conversion
            elif payload_key in ["total_amount", "discount_applied"]:
                val_str = str(value) if value is not None else "0"
                fields.append({"SchemaName": schema_name, "Value": val_str})
                
            # Handle standard fields
            else:
                if value is not None and value != "":
                    # Using str(value) ensures Enums or other types are stringified safely
                    fields.append({"SchemaName": schema_name, "Value": str(value)})

        return {
            "RelatedProspectId": lead_id,
            "ActivityEvent": activity_event_code,
            "ActivityNote": f"Order processing for {order_payload_dict.get('order_number', 'Unknown Order')}",
            "Fields": fields
        }

    async def push_activity(self, payload: Push_Activity) -> dict:
        """
        1. get or create lead
        2. push activity
        """
        order_data_dict = payload.order_data.model_dump()
        lead_id = await self.get_or_create_lead(order_data_dict)
        activity_event_code = self.LEADSQUARED_ACTIVITY_CODE_MAPPING.get(payload.activity)
        if not activity_event_code:
            return {"status": "failed", "message": f"Unknown activity type: {payload.activity}"}
        if not lead_id:
            return {"status": "failed", "message": "Lead ID (RelatedProspectId) is required."}

        lsq_payload = await self.build_payload(order_data_dict, lead_id, activity_event_code)
        endpoint = "/v2/ProspectActivity.svc/Create"
        return await self._make_request("POST", endpoint, json=lsq_payload)
    
    async def get_lead_by_phone(self, phone: str) -> dict:
        """
        1. Checks if a Lead exists by phone number.
        2. Returns the ProspectID if found.
        """
        get_endpoint = "LeadManagement.svc/RetrieveLeadByPhoneNumber"
        get_params = {"phone": phone}
        get_response = await self._await_request("GET", get_endpoint, params=get_params)
        return get_response

    async def create_lead(self, payload: CrmPayload.Lead) -> dict:
        """
        1. Creates a new Lead.
        2. Returns the ProspectID if found.
        """
        payload = payload.model_dump()
        create_endpoint = "LeadManagement.svc/Lead.Capture"
        
        # Base required data
        create_data = [
            {"Attribute": "Phone", "Value": payload.get("customer_phone")},
            {"Attribute": "FirstName", "Value": payload.get("customer_name", "Unknown Customer")},
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
            value = payload.get(payload_key)
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