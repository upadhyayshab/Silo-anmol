from enum import Enum


class UserRole(str, Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    WAREHOUSE_MANAGER = "WAREHOUSE_MANAGER"
    OUTLET_MANAGER = "OUTLET_MANAGER"
    TELECALLER = "TELECALLER"
    ACCOUNTANT = "ACCOUNTANT"


class OrderStatus(str, Enum):
    PENDING = "pending"
    DELIVERY_ALLOTTED = "delivery_allotted"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


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


class PayoutStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    PAID = "PAID"
    REJECTED = "REJECTED"


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

class LSQActivityField(str, Enum):
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
    ITEMS                = "mx_Custom_18" 
    REFUND_STATUS        = "mx_Custom_19"
    ACTUAL_DELIVERY_DATE = "mx_Custom_20"
    COUPON_CODE          = "mx_Custom_21"

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