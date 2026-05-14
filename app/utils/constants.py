from enum import Enum


class UserRole(str, Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    WAREHOUSE_MANAGER = "WAREHOUSE_MANAGER"
    OUTLET_MANAGER = "OUTLET_MANAGER"
    TELECALLER = "TELECALLER"
    ACCOUNTANT = "ACCOUNTANT"
    DELIVERY_GUY = "DELIVERY_GUY"


class OrderStatus(str, Enum):
    PENDING = "pending"
    DELIVERY_ALLOTTED = "delivery_allotted"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    POSTPONED = "postponed"
    # these are the status req from logistics
    ATTEMPTED = "attempted"
    CUSTOMER_NOT_AVAILABLE = "customer_not_available"
    UNABLE_TO_CONTACT = "unable_to_contact"
    UNABLE_TO_LOCATE = "unable_to_locate"
    PAYMENT_NOT_READY = "payment_not_ready"


class CollectionType(str, Enum):
    DOORSTEP = "doorstep"
    OUTLET_PICKUP = "outlet_pickup"


class PaymentMethod(str, Enum):
    CASH = "cash"
    ONLINE = "online"
    CARD = "card"
    UPI = "upi"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    PARTIALLY_PAID = "partially_paid"
    REFUNDED = "refunded"


class InvoiceType(str, Enum):
    REGULAR = "regular"
    RETURN = "return"
    CREDIT_NOTE = "credit_note"


class TransferStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class UnitOfMeasure(str, Enum):
    # UPPERCASE values (existing in database - for reading existing products)
    PIECE_UPPER = "PIECE"
    KG_UPPER = "KG"
    GRAM_UPPER = "GRAM"
    LITER_UPPER = "LITER"
    ML_UPPER = "ML"
    METER_UPPER = "METER"
    CM_UPPER = "CM"
    PACKET_UPPER = "PACKET"
    BOX_UPPER = "BOX"
    DOZEN_UPPER = "DOZEN"
    # lowercase values (for new products and frontend compatibility)
    piece = "piece"
    kg = "kg"
    gram = "gram"
    liter = "liter"
    ml = "ml"
    meter = "meter"
    cm = "cm"
    packet = "packet"
    box = "box"
    dozen = "dozen"
    bag = "bag"
    bottle = "bottle"
    can = "can"
    tablet = "tablet"
    sachet = "sachet"


class OutletPaymentMode(str, Enum):
    CASH = "CASH"
    BANK_DEPOSIT = "BANK_DEPOSIT"
    ONLINE = "ONLINE"
    UPI = "UPI"


class OutletPaymentSubMode(str, Enum):
    PHONEPE = "PHONEPE"
    GOOGLEPAY = "GOOGLEPAY"
    PAYTM = "PAYTM"
    NEFT = "NEFT"
    RTGS = "RTGS"
    IMPS = "IMPS"
    CASH = "CASH"


class OutletCollectionStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    NOT_RECEIVED = "NOT_RECEIVED"


class PayoutFrequency(str, Enum):
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"

class PayoutStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    PAID = "PAID"
    REJECTED = "REJECTED"

HASSAN_OUTLET_ID = "outlets_1bd14da6-954e-4f7d-bbf9-dafa4a6c3cf2"

__all__ = [
    "UserRole",
    "OrderStatus",
    "CollectionType",
    "PaymentMethod",
    "PaymentStatus",
    "InvoiceType",
    "TransferStatus",
    "UnitOfMeasure",
    "OutletPaymentMode",
    "OutletPaymentSubMode",
    "OutletCollectionStatus",
    "PayoutStatus",
    "PayoutFrequency",
    "HASSAN_OUTLET_ID"
]

class LeadSource(str, Enum):

    """get this lead source from - https://app-in21.leadsquared.com/Settings/ManageCustomFields?entityAttributeId=908f2dfc-29a5-11f1-868b-0a6a81ef4a11 """

    ORGANIC_SEARCH = "Organic Search"
    REFERRAL_SITES = "Referral Sites"
    DIRECT_TRAFFIC = "Direct Traffic"
    SOCIAL_MEDIA = "Social Media"
    INBOUND_EMAIL = "Inbound Email"
    INBOUND_PHONE_CALL = "Inbound Phone call"
    OUTBOUND_PHONE_CALL = "Outbound Phone call"
    PAY_PER_CLICK_ADS = "Pay per Click Ads"
    FB_LEAD_ADS = "FB Lead Ads"
    WEB_VISIT_LOGIN = "web visit/Login"
    WHATSAPP_INBOUND = "WhatsApp Inbound"    
    APP_SIGN_UP = "App sign up"
    GAU_SWASTH_SUBSCRIBER = "Gau swasth Subscriber"
    ADD_TO_CART = "add to cart"
    BROWSED_3_PAGES = "browsed 3 pages"

class LSQRefundStatusActivityField(str, Enum):
    ACTIVITY_EVENT_NOTE   = "ActivityEvent_Note"      
    REFUND_STATUS         = "Status"
    OWNER                 = "Owner"
    ORDER_ID              = "mx_Custom_1"
    PAYMENT_STATUS        = "mx_Custom_2"
    REFUND_AMOUNT         = "mx_Custom_3"
    REFUND_MODE           = "mx_Custom_4"
    REFUND_REFERENCE      = "mx_Custom_5"
    REFUND_PROCESSED_AT   = "mx_Custom_6"
    PROCESSED_BY          = "mx_Custom_7"
    NOTES                 = "mx_Custom_8"

class LSQPaymentStatusActivityField(str, Enum):
   ACTIVITY_EVENT_NOTE    = "ActivityEvent_Note"      
   PAYMENT_STATUS         = "Status"
   ORDER_ID               = "mx_Custom_1"
   AMOUNT_PAID            = "mx_Custom_2"
   TRANSACTION_REFERENCE  = "mx_Custom_3"
   PAYMENT_DATE           = "mx_Custom_4"
   AMOUNT_PAYABLE         = "mx_Custom_5"
   FAILURE_REASON         = "mx_Custom_6"
   FAILED_AT              = "mx_Custom_7"

class LSQDeliveryStatusActivityField(str, Enum):
    ACTIVITY_EVENT_NOTE   = "ActivityEvent_Note"      
    STATUS                = "Status"
    ORDER_ID              = "mx_Custom_1"
    OUTLET_NAME           = "mx_Custom_2"
    OUTLET_LOCATION       = "mx_Custom_3"
    OUTLET_PHONE          = "mx_Custom_4"
    OUTLET_MANAGER_NAME   = "mx_Custom_5"
    OUTLET_MANAGER_PHONE  = "mx_Custom_6"
    DELIVERY_AGENT_NAME   = "mx_Custom_7"
    DELIVERY_AGENT_PHONE  = "mx_Custom_8"
    EXPECTED_DELIVERY_DATE= "mx_Custom_9"
    DELIVERY_REMARKS      = "mx_Custom_10"
    ASSIGNED_AT           = "mx_Custom_12"
    DELIVERED_AT          = "mx_Custom_13"
    DELIVERY_NOTES        = "mx_Custom_14"
    TRANSACTION_REFERENCE = "mx_Custom_15"
    ORDER_STATUS          = "mx_Custom_16"
    PAYMENT_STATUS        = "mx_Custom_17"
    CREATED_AT            = "mx_Custom_18"
    ASSIGNMENT_PENDING_REASON = "mx_Custom_19" 
    RETURN_TYPE           = "mx_Custom_20"
    RETURN_REASON         = "mx_Custom_21"
    OUT_FOR_DELIVERY_AT   = "mx_Custom_22"
    RETURNED_AT           = "mx_Custom_23"
    REFUND_STATUS         = "mx_Custom_24"
    REFUND_AMOUNT         = "mx_Custom_25"

class LSQOrderStatusActivityField(str, Enum):
    ACTIVITY_EVENT_NOTE   = "ActivityEvent_Note"      
    ORDER_STATUS         = "Status"
    USER_ID              = "mx_Custom_1"
    ORDER_ID             = "mx_Custom_2"
    CUSTOMER_NAME        = "mx_Custom_3"
    CUSTOMER_PHONE       = "mx_Custom_4"
    CUSTOMER_EMAIL       = "mx_Custom_5"
    ADDRESS_LINE         = "mx_Custom_6"
    CITY                 = "mx_Custom_7"
    STATE                = "mx_Custom_8"
    PINCODE              = "mx_Custom_9"
    TOTAL_AMOUNT         = "mx_Custom_10"
    TOTAL_DISCOUNT       = "mx_Custom_11"
    PAYMENT_METHOD       = "mx_Custom_12"
    CREATED_AT           = "mx_Custom_13"
    INVOICE              = "mx_Custom_14"
    COUPON_CODE_STATUS   = "mx_Custom_15"
    DELIVERY_STATUS      = "mx_Custom_16"
    PAYMENT_STATUS       = "mx_Custom_17"
    PRODUCT_1            = "mx_Custom_18" 
    REFUND_STATUS        = "mx_Custom_19"
    ACTUAL_DELIVERY_DATE = "mx_Custom_20"
    COUPON_CODE          = "mx_Custom_21"
    REASON               = "mx_Custom_22"
    PRODUCT_2            = "mx_Custom_23"
    PRODUCT_3            = "mx_Custom_24"

class LSQProductField(str, Enum):
    PRODUCT_NAME         = "mx_CustomObject_1"
    PRODUCT_TITLE        = "mx_CustomObject_2"
    QUANTITY             = "mx_CustomObject_3"
    SIZE                 = "mx_CustomObject_4"
    UNIT_TYPE            = "mx_CustomObject_5"
    MRP                  = "mx_CustomObject_6"
    SELLING_PRICE        = "mx_CustomObject_7"
    DISCOUNT             = "mx_CustomObject_8"
    TOTAL_PRICE          = "mx_CustomObject_9"

class LSQCreateOrder(str, Enum):
    NOTES           = "ActivityEvent_Note"
    STATUS          = "Status"
    OWNER           = "Owner"
    ITEM_1          = "mx_Custom_1"
    ITEM_2          = "mx_Custom_2"
    ITEM_3          = "mx_Custom_3"
    NO_OF_ITEMS     = "mx_Custom_4"
    GRAND_TOTAL     = "mx_Custom_5"
    COLLECTION_TYPE = "mx_Custom_6"
    PINCODE         = "mx_Custom_7"
    ORDER_ID        = "mx_Custom_8"
    PREPAID_AMOUNT  = "mx_Custom_9"
    PAYMENT_METHOD  = "mx_Custom_10"
    LAT_LON         = "mx_Custom_11"
    SOURCE          = "mx_Custom_12"
    UTM_FIRST_TOUCH = "mx_Custom_14"
    UTM_LAST_TOUCH  = "mx_Custom_15"

class LSQItems(str, Enum):
    DISCOUNT_AMOUNT_PER_UNIT = "mx_CustomObject_1"
    PRODUCT_NAME             = "mx_CustomObject_2"
    CATEGORY                 = "mx_CustomObject_3"
    BRAND_NAME               = "mx_CustomObject_4"
    SKU_CODE                 = "mx_CustomObject_5"
    UNIT_TYPE                = "mx_CustomObject_6"
    SIZE                     = "mx_CustomObject_7"
    MRP                      = "mx_CustomObject_8"
    SELLING_PRICE            = "mx_CustomObject_9"
    QUANTITY                 = "mx_CustomObject_10"
    TOTAL_PRICE              = "mx_CustomObject_11"
    PRODUCT_ID               = "mx_CustomObject_12"
    PRODUCT_DESCRIPTION      = "mx_CustomObject_81"

class LSQUTMField(str, Enum):
    UTM_ID         = "mx_CustomObject_1"
    UTM_TERM       = "mx_CustomObject_2"
    TIMESTAMP      = "mx_CustomObject_3"
    SESSION_ID     = "mx_CustomObject_4"
    UTM_MEDIUM     = "mx_CustomObject_5"
    UTM_SOURCE     = "mx_CustomObject_6"
    UTM_CONTENT    = "mx_CustomObject_7"
    UTM_CAMPAIGN   = "mx_CustomObject_8"

class ActivityType(str, Enum):
    ORDER_STATUS = "order_status"
    PAYMENT_STATUS = "payment_status"
    REFUND_STATUS = "refund_status"
    DELIVERY_STATUS = "delivery_status"
    CREATE_ORDER = "create_order"
