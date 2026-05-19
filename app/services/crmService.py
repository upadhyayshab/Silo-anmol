import json
from typing import Any
from config import get_settings
from models import CrmPayload
from utils.crm_constants import ActivityType, LSQ_STATUS_MAP
from services.crm_activity_configs import ACTIVITY_REGISTRY, LSQ_ITEMS_FIELD_MAPPING, LSQ_ITEMS3_FIELD_MAPPING
from services.lsq_client import LeadSquaredClient


class CRMService:

    def __init__(self):
        settings = get_settings()
        self._client = LeadSquaredClient(
            base_url=settings.crm_url,
            access_key=settings.crm_api_key,
            secret_key=settings.crm_secret_key,
        )

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _get_nested_value(self, data: dict, path: str) -> Any:
        """Traverse a dot-notation path through a nested dict (e.g. 'a.b.c')."""
        for key in path.split("."):
            if isinstance(data, dict):
                data = data.get(key)
            else:
                return None
        return data

    def _build_custom_object_array(self, item_dict: dict, field_mapping: dict) -> list:
        """Convert a flat dict to an array of {SchemaName, Value} objects for LSQ."""
        custom_fields = []
        for item_key, schema_name in field_mapping.items():
            value = item_dict.get(item_key)
            if value is not None and value != "":
                schema_str = schema_name.value if hasattr(schema_name, "value") else str(schema_name)
                value_str = value.value if hasattr(value, "value") else str(value)
                custom_fields.append({"SchemaName": schema_str, "Value": value_str})
        return custom_fields

    def _map_lsq_status(self, key: str, value: str, activity_type: ActivityType | None = None) -> str:
        """Map an internal status string to the LeadSquared display value."""
        if key == "order_status" and activity_type == ActivityType.CREATE_ORDER:
            return LSQ_STATUS_MAP.get("order_status_create", {}).get(value.lower(), "Active")
        return LSQ_STATUS_MAP.get(key, {}).get(value.lower(), value)

    def _map_fields(self, payload_dict: dict, mapping: dict, activity_type: ActivityType) -> list[dict]:
        """Iterate a mapping dict and produce the LSQ Fields list."""
        fields = []
        for payload_key, schema_names in mapping.items():
            value = self._get_nested_value(payload_dict, payload_key)
            if not isinstance(schema_names, (list, tuple)):
                schema_names = [schema_names]
            for schema_name in schema_names:
                schema_str = schema_name.value if hasattr(schema_name, "value") else str(schema_name)
                if payload_key in ("total_amount", "discount_applied"):
                    fields.append({"SchemaName": schema_str, "Value": str(value) if value is not None else "0"})
                elif value is not None and value != "":
                    value_str = value.value if hasattr(value, "value") else str(value)
                    if payload_key in ("order_status", "payment_status", "delivery_status", "refund_status"):
                        value_str = self._map_lsq_status(payload_key, value_str, activity_type)
                    fields.append({"SchemaName": schema_str, "Value": value_str})
        return fields

    def _build_item_fields(self, payload_dict: dict, item_schemas: tuple, item_mapping: dict) -> list[dict]:
        """Serialize order items as nested LSQ custom objects (max 3 items)."""
        items_val = self._get_nested_value(payload_dict, "items")
        if not isinstance(items_val, list) or not items_val:
            return []
        fields = []
        for idx, raw_item in enumerate(items_val):
            if idx >= len(item_schemas):
                break
            product_info = raw_item.get("product") or {}
            if hasattr(product_info, "__dict__"):
                product_info = product_info.__dict__
            mrp_val = product_info.get("cost_price")
            if mrp_val is None or mrp_val == "":
                mrp_val = raw_item.get("cost_price")

            selling_price_val = raw_item.get("unit_price")
            if selling_price_val is None or selling_price_val == "":
                selling_price_val = raw_item.get("cost_price")

            discount_val = raw_item.get("product_manual_discount")
            if discount_val is None or discount_val == "":
                discount_val = raw_item.get("discount_amount")

            total_price_val = raw_item.get("subtotal")
            if total_price_val is None or total_price_val == "":
                total_price_val = raw_item.get("total_price")

            enriched_item = {
                "product_name":  product_info.get("lsq_display_name") or product_info.get("product_name"),
                "product_title": product_info.get("lsq_display_name") or product_info.get("product_name"),
                "quantity":      raw_item.get("quantity"),
                "size":          None,
                "unit_type":     product_info.get("unit_of_measure"),
                "mrp":           mrp_val,
                "selling_price": selling_price_val,
                "discount":      discount_val if discount_val is not None else 0.0,
                "total_price":   total_price_val,
            }
            # If this is Item 3 (idx == 2) and we're using LSQ_ITEMS_FIELD_MAPPING, switch to LSQ_ITEMS3_FIELD_MAPPING
            current_item_mapping = LSQ_ITEMS3_FIELD_MAPPING if (idx == 2 and item_mapping == LSQ_ITEMS_FIELD_MAPPING) else item_mapping
            inner_fields = self._build_custom_object_array(enriched_item, current_item_mapping)
            if inner_fields:
                schema_str = item_schemas[idx].value if hasattr(item_schemas[idx], "value") else str(item_schemas[idx])
                fields.append({"SchemaName": schema_str, "Value": "", "Fields": inner_fields})
        return fields

    # -------------------------------------------------------------------------
    # Payload building
    # -------------------------------------------------------------------------

    async def build_payload(self, data: dict, lead_id: str, activity_type: ActivityType) -> dict:
        config = ACTIVITY_REGISTRY[activity_type]

        fields = self._map_fields(data, config.mapping, activity_type)

        if config.item_schemas:
            fields.extend(self._build_item_fields(data, config.item_schemas, config.item_mapping))

        status_raw = data.get(config.status_key, "Unknown Status")
        status_str = status_raw.value if hasattr(status_raw, "value") else str(status_raw)
        status_str = self._map_lsq_status(config.status_key, status_str, activity_type)

        print("\n" + "=" * 60)
        print(f"LSQ PAYLOAD [{activity_type.value}]:")
        print({"RelatedProspectId": lead_id, "ActivityEvent": config.code, "Fields": fields})
        print("=" * 60 + "\n")

        return {
            "RelatedProspectId": lead_id,
            "ActivityEvent": config.code,
            "ActivityNote": f"{data.get('order_number', 'Unknown Order')} is {status_str}",
            "Fields": fields,
        }

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def push_activity(self, data: dict, activity_event: ActivityType) -> dict:
        """Get or create a lead, then push a CRM activity."""
        if activity_event not in ACTIVITY_REGISTRY:
            return {"status": "failed", "message": f"Unsupported activity type: {activity_event}"}

        lead_res = await self.get_or_create_lead(data)
        lead_id = lead_res.get("lead_id") if lead_res.get("status") == "success" else None
        if not lead_id:
            return {"status": "failed", "message": "Lead ID (RelatedProspectId) is required."}

        lsq_payload = await self.build_payload(data, lead_id, activity_event)
        return await self._client.request("POST", "ProspectActivity.svc/Create", json=lsq_payload)

    # -------------------------------------------------------------------------
    # Lead management
    # -------------------------------------------------------------------------

    async def get_lead_by_phone(self, phone: str) -> dict:
        return await self._client.request(
            "GET",
            "LeadManagement.svc/RetrieveLeadByPhoneNumber",
            params={"phone": phone},
        )

    async def create_lead(self, payload: dict) -> dict:
        try:
            validated_lead = CrmPayload.Lead(**payload)
        except Exception as e:
            print(f"Validation failed for Lead payload: {e}")
            return {"status": "failed", "message": "Invalid lead data format."}

        valid_payload = validated_lead.model_dump(exclude_none=True)

        create_data = [
            {"Attribute": "Phone", "Value": valid_payload.get("customer_phone")},
            {"Attribute": "FirstName", "Value": valid_payload.get("customer_name", "Unknown Customer")},
            {"Attribute": "SearchBy", "Value": "Phone"},
        ]

        field_mapping = {
            "customer_email": "EmailAddress",
            "Source":         "Source",
            "LastName":       "LastName",
            "Created_On":     "CreatedOn",
            "State":          "mx_State",
            "Country":        "mx_Country",
            "address_line_1": "mx_Street1",
            "address_line_2": "mx_Street2",
            "city":           "mx_City",
        }
        for key, attr in field_mapping.items():
            value = valid_payload.get(key)
            if value:
                create_data.append({"Attribute": attr, "Value": value})

        response = await self._client.request(
            "POST",
            "LeadManagement.svc/Lead.Capture",
            json=create_data,
            params={"LeadUpdateBehavior": "DoNotUpdate"},
        )

        if isinstance(response, dict) and response.get("Status") == "Success":
            message_obj = response.get("Message", {})
            lead_id = message_obj.get("RelatedId") if isinstance(message_obj, dict) else message_obj or None
            return {"status": "success", "lead_id": lead_id, "action": "created"}
        return {"status": "failed", "message": "Failed to create lead", "details": response}

    async def get_lsq_telecallers(self):
        response = await self._client.request("GET", "UserManagement.svc/Users.Get")
        if isinstance(response, list):
            return [user for user in response if user.get("Role") == "Sales_User"]
        return response

    async def get_or_create_lead(self, payload: dict) -> dict:
        phone = payload.get("customer_phone")
        if not phone:
            return {"status": "failed", "message": "customer_phone is required to find or create a lead."}

        get_response = await self.get_lead_by_phone(phone)
        if isinstance(get_response, list) and get_response:
            return {"status": "success", "lead_id": get_response[0].get("ProspectID"), "action": "retrieved"}

        create_response = await self.create_lead(payload)
        if create_response.get("status") == "success":
            return create_response
        return {"status": "failed", "message": "Failed to retrieve or create lead.", "crm_response": create_response}

    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------

    def clean_lsq_payload(self, data):
        if isinstance(data, dict):
            cleaned = {}
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
                cleaned[k] = v
            return cleaned
        elif isinstance(data, list):
            return [self.clean_lsq_payload(item) for item in data if item is not None]
        return data

    async def push_to_crm(self, payload: dict) -> dict:
        pass

    async def pull_from_crm(self) -> dict:
        pass
