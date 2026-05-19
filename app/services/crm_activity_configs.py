from dataclasses import dataclass, field
from utils.crm_constants import (
    ActivityType,
    LSQDeliveryStatusActivityField,
    LSQOrderStatusActivityField,
    LSQPaymentStatusActivityField,
    LSQCreateOrder,
    LSQItems,
    LSQUTMField,
    LSQProductField,
)


@dataclass(frozen=True)
class ActivityConfig:
    code: int
    mapping: dict
    status_key: str
    # item_schemas: non-empty only for activity types that serialize order items
    item_schemas: tuple = field(default_factory=tuple)
    item_mapping: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Field mapping dicts: internal payload key → LSQ SchemaName(s)
# A value can be a single enum member or a tuple (maps one key to many fields).
# ---------------------------------------------------------------------------

DELIVERY_STATUS_MAPPING: dict = {
    "activity_note":                    LSQDeliveryStatusActivityField.ACTIVITY_EVENT_NOTE,
    "delivery_status":                  (LSQDeliveryStatusActivityField.STATUS, LSQDeliveryStatusActivityField.ORDER_STATUS),
    "order_number":                     LSQDeliveryStatusActivityField.ORDER_ID,
    "assigned_outlet.outlet_name":      LSQDeliveryStatusActivityField.OUTLET_NAME,
    "assigned_outlet.address":          LSQDeliveryStatusActivityField.OUTLET_LOCATION,
    "assigned_outlet.phone":            LSQDeliveryStatusActivityField.OUTLET_PHONE,
    "assigned_outlet.manager.full_name": LSQDeliveryStatusActivityField.OUTLET_MANAGER_NAME,
    "assigned_outlet.manager.phone":    LSQDeliveryStatusActivityField.OUTLET_MANAGER_PHONE,
    "delivery_guy.full_name":           LSQDeliveryStatusActivityField.DELIVERY_AGENT_NAME,
    "delivery_guy.phone":               LSQDeliveryStatusActivityField.DELIVERY_AGENT_PHONE,
    "expected_delivery_date":           LSQDeliveryStatusActivityField.EXPECTED_DELIVERY_DATE,
    "delivery_remarks":                 LSQDeliveryStatusActivityField.DELIVERY_REMARKS,
    "assigned_at":                      LSQDeliveryStatusActivityField.ASSIGNED_AT,
    "actual_delivery_date":             LSQDeliveryStatusActivityField.DELIVERED_AT,
    "status_remarks":                   LSQDeliveryStatusActivityField.DELIVERY_NOTES,
    "transaction_reference":            LSQDeliveryStatusActivityField.TRANSACTION_REFERENCE,
    "payment_status":                   LSQDeliveryStatusActivityField.PAYMENT_STATUS,
    "created_at":                       LSQDeliveryStatusActivityField.CREATED_AT,
    "assignment_pending_reason":        LSQDeliveryStatusActivityField.ASSIGNMENT_PENDING_REASON,
    "return_type":                      LSQDeliveryStatusActivityField.RETURN_TYPE,
    "return_reason":                    LSQDeliveryStatusActivityField.RETURN_REASON,
    "out_for_delivery_at":              LSQDeliveryStatusActivityField.OUT_FOR_DELIVERY_AT,
    "returned_at":                      LSQDeliveryStatusActivityField.RETURNED_AT,
    "refund_status":                    LSQDeliveryStatusActivityField.REFUND_STATUS,
    "refund_amount":                    LSQDeliveryStatusActivityField.REFUND_AMOUNT,
}

ORDER_STATUS_MAPPING: dict = {
    "order_status":         LSQOrderStatusActivityField.ORDER_STATUS,
    "user_id":              LSQOrderStatusActivityField.USER_ID,
    "uid":                  LSQOrderStatusActivityField.ORDER_ID,
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
    "refund_status":        LSQOrderStatusActivityField.REFUND_STATUS,
    "actual_delivery_date": LSQOrderStatusActivityField.ACTUAL_DELIVERY_DATE,
    "coupon_code":          LSQOrderStatusActivityField.COUPON_CODE,
    "status_remarks":       LSQOrderStatusActivityField.REASON,
}

PAYMENT_STATUS_MAPPING: dict = {
    "activity_note":         LSQPaymentStatusActivityField.ACTIVITY_EVENT_NOTE,
    "payment_status":        LSQPaymentStatusActivityField.PAYMENT_STATUS,
    "order_id":              LSQPaymentStatusActivityField.ORDER_ID,
    "amount_paid":           LSQPaymentStatusActivityField.AMOUNT_PAID,
    "transaction_reference": LSQPaymentStatusActivityField.TRANSACTION_REFERENCE,
    "payment_date":          LSQPaymentStatusActivityField.PAYMENT_DATE,
    "amount_payable":        LSQPaymentStatusActivityField.AMOUNT_PAYABLE,
    "failure_reason":        LSQPaymentStatusActivityField.FAILURE_REASON,
    "failed_at":             LSQPaymentStatusActivityField.FAILED_AT,
}

CREATE_ORDER_MAPPING: dict = {
    "status_remarks":   LSQCreateOrder.NOTES,
    "order_status":     LSQCreateOrder.STATUS,
    "owner":            LSQCreateOrder.OWNER,
    "no_of_items":      LSQCreateOrder.NO_OF_ITEMS,
    "grand_total":      LSQCreateOrder.GRAND_TOTAL,
    "collection_type":  LSQCreateOrder.COLLECTION_TYPE,
    "order_id":         LSQCreateOrder.ORDER_ID,
    "pincode":          LSQCreateOrder.PINCODE,
    "prepaid_amount":   LSQCreateOrder.PREPAID_AMOUNT,
    "payment_method":   LSQCreateOrder.PAYMENT_METHOD,
    "lat_lon":          LSQCreateOrder.LAT_LON,
    "source":           LSQCreateOrder.SOURCE,
    "utm_first_touch":  LSQCreateOrder.UTM_FIRST_TOUCH,
    "utm_last_touch":   LSQCreateOrder.UTM_LAST_TOUCH,
}

# Maps readable field names → LSQ custom object schema names for order items.
# Used by _build_custom_object_array when serializing a single item.
LSQ_ITEMS_FIELD_MAPPING: dict = {
    "discount_amount_per_unit": LSQItems.DISCOUNT_AMOUNT_PER_UNIT,
    "product_name":             LSQItems.PRODUCT_NAME,
    "category":                 LSQItems.CATEGORY,
    "brand_name":               LSQItems.BRAND_NAME,
    "sku_code":                 LSQItems.SKU_CODE,
    "unit_type":                LSQItems.UNIT_TYPE,
    "size":                     LSQItems.SIZE,
    "mrp":                      LSQItems.MRP,
    "selling_price":            LSQItems.SELLING_PRICE,
    "quantity":                 LSQItems.QUANTITY,
    "total_price":              LSQItems.TOTAL_PRICE,
    "product_id":               LSQItems.PRODUCT_ID,
    "product_description":      LSQItems.PRODUCT_DESCRIPTION,
}

# Maps readable field names -> LSQ custom object schema names for product items in ORDER_STATUS (code 203).
# Specifically aligns with the LSQ product UI where mx_CustomObject_8 is DISCOUNT and mx_CustomObject_6 is MRP.
LSQ_PRODUCT_FIELD_MAPPING: dict = {
    "product_name":             LSQProductField.PRODUCT_NAME,
    "product_title":            LSQProductField.PRODUCT_TITLE,
    "quantity":                 LSQProductField.QUANTITY,
    "size":                     LSQProductField.SIZE,
    "unit_type":                LSQProductField.UNIT_TYPE,
    "mrp":                      LSQProductField.MRP,
    "selling_price":            LSQProductField.SELLING_PRICE,
    "discount":                 LSQProductField.DISCOUNT,
    "total_price":              LSQProductField.TOTAL_PRICE,
}

LSQ_UTM_FIELD_MAPPING: dict = {
    "utm_id":       LSQUTMField.UTM_ID,
    "utm_term":     LSQUTMField.UTM_TERM,
    "timestamp":    LSQUTMField.TIMESTAMP,
    "session_id":   LSQUTMField.SESSION_ID,
    "utm_medium":   LSQUTMField.UTM_MEDIUM,
    "utm_source":   LSQUTMField.UTM_SOURCE,
    "utm_content":  LSQUTMField.UTM_CONTENT,
    "utm_campaign": LSQUTMField.UTM_CAMPAIGN,
}


# ---------------------------------------------------------------------------
# Registry: single source of truth for all CRM activity types.
# To add a new activity type: add one entry here + one mapping dict above.
# ---------------------------------------------------------------------------

ACTIVITY_REGISTRY: dict[ActivityType, ActivityConfig] = {
    ActivityType.DELIVERY_STATUS: ActivityConfig(
        code=206,
        mapping=DELIVERY_STATUS_MAPPING,
        status_key="delivery_status",
    ),
    ActivityType.ORDER_STATUS: ActivityConfig(
        code=203,
        mapping=ORDER_STATUS_MAPPING,
        status_key="order_status",
        item_schemas=(
            LSQOrderStatusActivityField.PRODUCT_1,
            LSQOrderStatusActivityField.PRODUCT_2,
            LSQOrderStatusActivityField.PRODUCT_3,
        ),
        item_mapping=LSQ_PRODUCT_FIELD_MAPPING,
    ),
    ActivityType.PAYMENT_STATUS: ActivityConfig(
        code=205,
        mapping=PAYMENT_STATUS_MAPPING,
        status_key="payment_status",
    ),
    ActivityType.CREATE_ORDER: ActivityConfig(
        code=209,
        mapping=CREATE_ORDER_MAPPING,
        status_key="order_status",
        item_schemas=(
            LSQCreateOrder.ITEM_1,
            LSQCreateOrder.ITEM_2,
            LSQCreateOrder.ITEM_3,
        ),
        item_mapping=LSQ_ITEMS_FIELD_MAPPING,
    ),
}
