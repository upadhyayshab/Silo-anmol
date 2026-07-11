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
    # Additive: existing JWTs/rows are unaffected. `users.role` is a varchar
    # column, not a Postgres ENUM, so any role name — built-in or custom — is
    # assignable without a migration; existence is validated at the app layer
    # against the `roles` table.
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


# Roles that work CRM leads as telecallers (auto-assignment, distribute, inbound).
# In-house TELECALLERs and agency AGENCY_TELECALLERs are pooled identically for lead
# routing; they keep distinct RBAC/agency-management rules elsewhere. A list (not a
# tuple) so it drops straight into manager `.in_()` filters.
TELECALLER_ROLES = [UserRole.TELECALLER, UserRole.AGENCY_TELECALLER]

# Roles a lead's owner may hold. Superset of TELECALLER_ROLES: a super admin may
# also hand leads to AGENCY_ADMINs (they oversee/close leads for their agency) via
# the Change Owner popup. Agency admins are never in the *auto* round-robin pool.
OWNER_ROLES = TELECALLER_ROLES + [UserRole.AGENCY_ADMIN]


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


# Dead-end states — an order here is done and never auto-reverted.
TERMINAL_ORDER_STATUSES = (OrderStatus.DELIVERED, OrderStatus.CANCELLED)


def is_revertible_status(order_status: OrderStatus) -> bool:
    """True for a non-terminal, non-pending status — these auto-revert to PENDING
    after 24h untouched (see the daily revert job). Defined by exclusion so any
    status added later is covered automatically."""
    return order_status not in (OrderStatus.PENDING, *TERMINAL_ORDER_STATUSES)


# Seeded inactive account used as `changed_by` for automated (non-human) status
# changes — e.g. the nightly aging auto-cancel job. See scripts/seed/seed_system_user.py.
SYSTEM_USER_UID = "users_system_autorevert"


# app_settings keys (one row per setting) backing the retired daily order auto-revert
# job (app/services/order_revert_service.py, removed 2026-07-11). The superadmin
# toggle in app/routers/v1/config.py still reads/writes these — left dead pending
# its own removal (see Task 12) — so the keys stay here rather than dangling imports.
SETTING_AUTO_REVERT_ENABLED = "auto_revert_enabled"
SETTING_AUTO_REVERT_FLUSH = "auto_revert_flush_existing"
SETTING_AUTO_REVERT_BASELINE_AT = "auto_revert_baseline_at"  # ISO-8601 string


class OrderEventType(str, Enum):
    CREATED = "CREATED"
    ASSIGNED = "ASSIGNED"
    STATUS_CHANGE = "STATUS_CHANGE"
    RIDER_DISPOSITION = "RIDER_DISPOSITION"
    RETURNED_TO_OUTLET = "RETURNED_TO_OUTLET"
    ESCALATED_CRM = "ESCALATED_CRM"
    CRM_OUTCOME = "CRM_OUTCOME"
    ESCALATED_LOGISTICS = "ESCALATED_LOGISTICS"
    CANCELLED = "CANCELLED"
    EDITED = "EDITED"
    DELETED = "DELETED"


class CancellationReason(str, Enum):
    CUSTOMER_DECLINED = "CUSTOMER_DECLINED"
    AGED_OUT = "AGED_OUT"
    DUPLICATE = "DUPLICATE"
    OUT_OF_SERVICE_AREA = "OUT_OF_SERVICE_AREA"
    MANUAL_OTHER = "MANUAL_OTHER"


class EscalationState(str, Enum):
    NONE = "NONE"
    CRM_REVIEW = "CRM_REVIEW"
    LOGISTICS = "LOGISTICS"


class Custody(str, Enum):
    OUTLET = "OUTLET"
    RIDER = "RIDER"
    CUSTOMER = "CUSTOMER"


# Rider dispositions that count as a failed delivery attempt (drive escalation).
# 'delivered' is excluded. Values match the rider-app/webhook status strings.
NON_DELIVERED_RIDER_OUTCOMES = frozenset({
    "attempted", "customer_not_available", "unable_to_contact",
    "unable_to_locate", "payment_not_ready", "postponed",
})

# app_settings kill-switch for the nightly 30-day auto-cancel job (default off).
SETTING_AGING_CANCEL_ENABLED = "aging_cancel_enabled"

# Telephony: outbound caller-ID (the Exotel usermapping VirtualNumber) per telecaller
# state. Also app_settings rows -> no migration. A missing row falls back to
# DEFAULT_EXOPHONE, so shipping with no config changes nothing.
SETTING_DEFAULT_EXOPHONE = "default_exophone"   # str  — used when a state has no override
SETTING_STATE_EXOPHONES = "state_exophones"     # dict — {state: exophone}
DEFAULT_EXOPHONE = "+918068875144"              # the ExoPhone 83/109 agents already use

# Temporary IVR bridge: one published number today, so the caller picks their language and
# the chosen digit stands in for the region. {digit: exophone} — the digit is an ALIAS for
# a number already in SETTING_STATE_EXOPHONES, so region membership has a single source of
# truth. Delete the App Bazaar Gather applet once the 3 numbers are live and this key stops
# being read (the dialed number supplies the region directly).
SETTING_IVR_DIGITS = "ivr_digits"               # dict — {"1": "+9180...", "2": ...}


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


def payment_status_for(amount_paid, order_value) -> "PaymentStatus":
    """Status for a payment recorded at order-creation: PAID only when it covers the order's
    (post-discount) value, else PARTIALLY_PAID. Money path — pinned by test_order_payment_status."""
    return PaymentStatus.PAID if amount_paid >= order_value else PaymentStatus.PARTIALLY_PAID


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
