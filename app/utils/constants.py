from enum import Enum


class UserRole(str, Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    WAREHOUSE_MANAGER = "WAREHOUSE_MANAGER"
    OUTLET_MANAGER = "OUTLET_MANAGER"
    TELECALLER = "TELECALLER"
    ACCOUNTANT = "ACCOUNTANT"
    DELIVERY_GUY = "DELIVERY_GUY"
    # --- RBAC blueprint roles (see utils/permissions.ROLE_DEFINITIONS) ---
    # Additive: existing JWTs/rows are unaffected. Assigning one of these to a
    # user requires a Postgres ENUM `ADD VALUE` migration first (Phase 2).
    VIEWER = "VIEWER"                          # L1 read-only
    CLUSTER_MANAGER = "CLUSTER_MANAGER"        # L3 ops, cluster scope
    STATE_HEAD = "STATE_HEAD"                  # L4 oversight, state scope
    MARKETING_EXECUTIVE = "MARKETING_EXECUTIVE"
    MARKETING_HEAD = "MARKETING_HEAD"
    FINANCE_LEAD = "FINANCE_LEAD"              # L4 finance, read-only
    AUDITOR = "AUDITOR"                        # L4 read-only + audit logs
    # Leadership (read-only, global, function-filtered)
    CEO = "CEO"
    CFO = "CFO"
    COO = "COO"
    CGO = "CGO"
    # Admins (function-scoped write/admin)
    USER_ADMIN = "USER_ADMIN"
    CATALOGUE_ADMIN = "CATALOGUE_ADMIN"
    CONFIG_ADMIN = "CONFIG_ADMIN"
    OPS_ADMIN = "OPS_ADMIN"
    FINANCE_ADMIN = "FINANCE_ADMIN"
    # External calling agencies (see agency-grouping design)
    AGENCY_TELECALLER = "AGENCY_TELECALLER"
    AGENCY_ADMIN = "AGENCY_ADMIN"


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


class OutletType(str, Enum):
    FACTORY   = "factory"
    WAREHOUSE = "warehouse"
    OUTLET    = "outlet"


class SmartpingJobStatus(str, Enum):
    PENDING = "PENDING"
    LOCKED = "LOCKED"
    SENT = "SENT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AuditStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"  # Auto-set on the cycle's Wednesday deadline if never submitted


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
    "OutletType",
    "SmartpingJobStatus",
    "AuditStatus",
]
