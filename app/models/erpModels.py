from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from decimal import Decimal
import re

from utils.constants import (
    UserRole, OrderStatus, CollectionType, PaymentMethod,
    PaymentStatus, InvoiceType, TransferStatus, UnitOfMeasure,
    OutletPaymentMode, OutletPaymentSubMode, OutletCollectionStatus,
    PayoutStatus, PayoutFrequency, OutletType, AuditStatus
)


# ============================================================================
# USER MODELS
# ============================================================================

class UserCreateRequest(BaseModel):
    email: str = Field(..., description="User email address")
    password: str = Field(..., min_length=8)
    full_name: str
    role: UserRole
    phone: Optional[str] = None
    outlet_id: Optional[str] = None
    agency_id: Optional[str] = None
    assignment_quota: Optional[int] = Field(default=0, description="Max leads per day or active pool for telecallers")

    @validator('email')
    def validate_email(cls, v):
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, v):
            raise ValueError('Invalid email format')
        return v.lower()


class UserUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    outlet_id: Optional[str] = None
    is_active: Optional[bool] = None
    agency_id: Optional[str] = None
    assignment_quota: Optional[int] = None


class UserPasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8)


class UserResponse(BaseModel):
    uid: str
    email: str
    full_name: str
    role: UserRole
    phone: Optional[str] = None
    outlet_id: Optional[str] = None
    agency_id: Optional[str] = None
    state: Optional[str] = None
    is_active: bool
    assignment_quota: Optional[int] = 0
    last_login: Optional[datetime] = None
    last_active_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ============================================================================
# OUTLET MODELS
# ============================================================================

class AgencyCreateRequest(BaseModel):
    name: str


class AgencyResponse(BaseModel):
    uid: str
    name: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class OutletCreateRequest(BaseModel):
    outlet_name: str
    outlet_code: str = Field(..., max_length=50)
    address: str
    city: str
    state: str
    pincode: str = Field(..., max_length=10)
    phone: str
    email: Optional[str] = None
    gstin: str = Field(..., max_length=15)
    state_code: str = Field(..., max_length=2)
    pan: str = Field(..., max_length=10)
    lat_lon: Optional[List[Decimal]] = None
    manager_id: Optional[str] = None
    outlet_type: OutletType = OutletType.OUTLET


class OutletUpdateRequest(BaseModel):
    outlet_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    gstin: Optional[str] = None
    state_code: Optional[str] = None
    pan: Optional[str] = None
    lat_lon: Optional[List[Decimal]] = None
    manager_id: Optional[str] = None
    is_active: Optional[bool] = None
    outlet_type: Optional[OutletType] = None


class OutletResponse(BaseModel):
    uid: str
    outlet_name: str
    outlet_code: str
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    gstin: Optional[str] = None
    state_code: Optional[str] = None
    pan: Optional[str] = None
    lat_lon: Optional[List[Decimal]] = None
    manager_id: Optional[str] = None
    is_active: bool
    outlet_type: OutletType = OutletType.OUTLET
    created_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# PRODUCT MODELS
# ============================================================================

class ProductCategoryCreateRequest(BaseModel):
    category_name: str
    description: Optional[str] = None


class ProductCategoryResponse(BaseModel):
    uid: str
    category_name: str
    description: Optional[str] = None
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ProductCreateRequest(BaseModel):
    sku: str
    product_name: str
    description: Optional[str] = None
    category_id: str
    hsn_code: str = Field(..., max_length=8)
    tax_rate: Decimal = Field(..., ge=0, le=100)
    unit_price: Decimal = Field(..., gt=0)
    cost_price: Decimal = Field(..., gt=0)
    unit_of_measure: UnitOfMeasure
    barcode: Optional[str] = None
    image_url: Optional[str] = None
    min_stock_level: int = Field(default=10, ge=0)
    lsq_display_name: Optional[str] = Field(None, description="Display name for LeadSquared")
    commission: Decimal = Field(default=0.00, ge=0, description="Commission in rupees")
    discount: Decimal = Field(default=0.00, ge=0, description="Discount in rupees")
    margin: Decimal = Field(default=Decimal("0.00"), ge=0, description="Margin for non-Silo Fortune products")


class ProductUpdateRequest(BaseModel):
    product_name: Optional[str] = None
    description: Optional[str] = None
    category_id: Optional[str] = None
    hsn_code: Optional[str] = None
    tax_rate: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    cost_price: Optional[Decimal] = None
    unit_of_measure: Optional[UnitOfMeasure] = None
    barcode: Optional[str] = None
    image_url: Optional[str] = None
    min_stock_level: Optional[int] = None
    lsq_display_name: Optional[str] = Field(None, description="Display name for LeadSquared")
    commission: Optional[Decimal] = Field(None, ge=0, description="Commission in rupees")
    discount: Optional[Decimal] = Field(None, ge=0, description="Discount in rupees")
    margin: Optional[Decimal] = Field(None, ge=0, description="Margin for non-Silo Fortune products")
    is_active: Optional[bool] = None


class ProductResponse(BaseModel):
    uid: str
    sku: str
    product_name: str
    description: Optional[str] = None
    category_id: str
    hsn_code: str
    tax_rate: Decimal
    unit_price: Decimal
    # Masked (nulled) for roles without products:cost:read — see utils.permissions.MASKED_COLUMNS
    cost_price: Optional[Decimal] = None
    unit_of_measure: UnitOfMeasure
    barcode: Optional[str] = None
    image_url: Optional[str] = None
    min_stock_level: int
    lsq_display_name: Optional[str] = None
    commission: Optional[Decimal] = None  # masked with cost_price
    discount: Decimal
    margin: Optional[Decimal] = None       # masked with cost_price
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# INVENTORY MODELS
# ============================================================================

class InventoryResponse(BaseModel):
    uid: str
    product_id: str
    outlet_id: Optional[str] = None
    quantity: int = Field(..., description="Total physical stock available at the outlet")
    reserved_quantity: int = Field(0, description="[DEPRECATED] Previously used for pending orders, now always 0")
    available_quantity: int = Field(..., description="[DEPRECATED] Equal to quantity in the simplified system")
    last_updated: datetime
    total_received: int = 0
    delivered: int = 0

    class Config:
        from_attributes = True


class InventoryAuditResponse(InventoryResponse):
    db_quantity: int
    audited_quantity: int
    total_transferred_out: int = 0


class StockAdjustmentRequest(BaseModel):
    product_id: str
    outlet_id: Optional[str] = None  # NULL for warehouse
    quantity_change: int  # Positive for addition, negative for reduction
    reason: str


class WeeklyInventoryAuditItemResponse(BaseModel):
    uid: str
    product_id: str
    product_name: Optional[str] = None
    system_quantity: Optional[int] = 0
    physical_quantity: Optional[int] = None

    class Config:
        from_attributes = True

class WeeklyInventoryAuditResponse(BaseModel):
    uid: str
    outlet_id: str
    outlet_name: Optional[str] = None
    audit_date: date
    week_start: Optional[date] = None
    iso_week: Optional[int] = None
    iso_year: Optional[int] = None
    week_label: Optional[str] = None
    status: AuditStatus
    match_percentage: Optional[Decimal] = None
    submitted_at: Optional[datetime] = None
    submitted_by: Optional[str] = None
    items: List[WeeklyInventoryAuditItemResponse] = []

    class Config:
        from_attributes = True

class WeeklyInventoryAuditSubmitItem(BaseModel):
    product_id: str
    physical_quantity: int

class WeeklyInventoryAuditSubmitRequest(BaseModel):
    items: List[WeeklyInventoryAuditSubmitItem]

class AuditOutletRef(BaseModel):
    outlet_id: str
    outlet_name: Optional[str] = None

class AuditCycleRef(BaseModel):
    week_start: date
    week_label: Optional[str] = None
    audit_count: int = 0

class WeeklyInventoryAuditSummaryResponse(BaseModel):
    total_active_outlets: int
    completed_audits: int
    average_match_percentage: Optional[Decimal] = None
    week_start: Optional[date] = None
    week_label: Optional[str] = None
    completed_outlets: List[AuditOutletRef] = []
    pending_outlets: List[AuditOutletRef] = []

class WeeklyInventoryAuditItemReportResponse(BaseModel):
    uid: str
    audit_date: date
    week_start: Optional[date] = None
    iso_week: Optional[int] = None
    iso_year: Optional[int] = None
    week_label: Optional[str] = None
    outlet_name: Optional[str] = None
    status: AuditStatus
    match_percentage: Optional[Decimal] = None
    submitted_at: Optional[datetime] = None
    submitted_by_name: Optional[str] = None
    product_name: Optional[str] = None
    system_quantity: Optional[int] = 0
    physical_quantity: Optional[int] = None
    unit_price: Optional[Decimal] = None

    class Config:
        from_attributes = True


# ============================================================================
# ORDER MODELS
# ============================================================================

class OrderItemRequest(BaseModel):
    product_id: str
    quantity: int = Field(..., gt=0)
    product_manual_discount: Decimal = Field(default=Decimal('0.00'), ge=0, description="Manual discount for this product in rupees")
    # unit_price removed - now calculated automatically from product.cost_price - product_manual_discount


class OrderPaymentRequest(BaseModel):
    """Prepaid payment captured at order-creation time. The backend records this transaction
    in the SAME DB commit as the order, so an order can never persist without the payment that
    was taken for it. `payment_status` is computed server-side; `order_id` is implicit."""
    payment_method: PaymentMethod
    amount_paid: Decimal = Field(..., gt=0)
    transaction_reference: Optional[str] = None
    notes: Optional[str] = None

    @validator('transaction_reference', always=True)
    def validate_transaction_reference(cls, v, values):
        # always=True: must also run when the field is omitted entirely (defaults to None) —
        # cash/COD payments don't carry a transaction id; upi/online/card are prepaid methods
        # and must be traceable to a real payment.
        method = values.get('payment_method')
        if method in (PaymentMethod.UPI, PaymentMethod.ONLINE, PaymentMethod.CARD):
            if not v or not v.strip():
                raise ValueError('transaction_reference is required for prepaid payments')
        return v


class OrderCreateRequest(BaseModel):
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
    lat_lon: Optional[List[Decimal]] = None
    collection_type: CollectionType
    payment_method: PaymentMethod
    expected_delivery_date: Optional[date] = None
    manual_discount: Decimal = Field(default=Decimal('0.00'), ge=0, description="Manual discount in rupees for entire order")
    prepaid_amount: Decimal = Field(default=Decimal('0.00'), ge=0, description="Amount already paid in advance")
    priority_level: int = Field(default=10, description="Order priority level (default 10 for normal)")
    lead_id: Optional[str] = Field(default=None, description="CRM lead this order was placed from (links order to the lead timeline)")
    source: Optional[str] = Field(default=None, description="Source of the order (e.g. FB Lead Ads, Organic Search, etc.)")
    payment: Optional[OrderPaymentRequest] = Field(default=None, description="Prepaid payment to record atomically with the order (backend-owned; replaces the old second frontend call)")
    items: List[OrderItemRequest]


class ProxyOrderCreateRequest(OrderCreateRequest):
    telecaller_id: str = Field(..., description="ID of telecaller to create order on behalf of")


class OrderUpdateRequest(BaseModel):
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    address_line: Optional[str] = None
    village: Optional[str] = None
    post: Optional[str] = None
    hobli: Optional[str] = None
    taluk: Optional[str] = None
    district: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    lat_lon: Optional[List[Decimal]] = None
    expected_delivery_date: Optional[date] = None
    priority_level: Optional[int] = None


class OrderFullUpdateRequest(BaseModel):
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
    lat_lon: Optional[List[Decimal]] = None
    collection_type: CollectionType
    payment_method: PaymentMethod
    expected_delivery_date: Optional[date] = None
    manual_discount: Decimal = Field(default=Decimal('0.00'), ge=0, description="Manual discount in rupees for entire order")
    prepaid_amount: Decimal = Field(default=Decimal('0.00'), ge=0, description="Amount already paid in advance") # Added field
    priority_level: int = Field(default=10, description="Order priority level (default 10 for normal)")
    source: Optional[str] = Field(default=None, description="Source of the order")
    items: List[OrderItemRequest]


class OrderStatusUpdateRequest(BaseModel):
    order_status: OrderStatus
    status_remarks: Optional[str] = None  # Mandatory for cancelled
    postpone_date: Optional[date] = None


class OrderAssignRequest(BaseModel):
    assigned_outlet_id: str


class OrderRevokeRequest(BaseModel):
    reason: str = Field(..., min_length=10, description="Reason for revoking the order (minimum 10 characters)")


class ProductBrief(BaseModel):
    uid: str
    product_name: str
    sku: str

    class Config:
        from_attributes = True


class OrderItemResponse(BaseModel):
    uid: str
    product_id: str
    quantity: int
    unit_price: Decimal
    subtotal: Decimal
    product_manual_discount: Decimal
    product: Optional[ProductBrief] = None
    inventory_quantity: Optional[int] = None

    class Config:
        from_attributes = True


class OrderResponse(BaseModel):
    uid: str
    order_number: str
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
    lat_lon: Optional[List[Decimal]] = None
    telecaller_id: str
    agency_id: Optional[str] = None
    assigned_outlet_id: Optional[str] = None
    source: Optional[str] = None
    order_status: OrderStatus
    collection_type: CollectionType
    payment_method: PaymentMethod
    order_date: datetime
    expected_delivery_date: Optional[date] = None
    actual_delivery_date: Optional[datetime] = None
    status_remarks: Optional[str] = None
    gross_amount: Decimal  # Total before manual discount
    manual_discount: Decimal  # Manual discount applied (legacy field, kept for compatibility)
    discount_applied: Decimal  # Total discount (product manual discounts)
    prepaid_amount: Decimal  # Amount already paid
    total_amount: Decimal  # Final net amount
    total_commission: Optional[Decimal] = None  # Total commission for the order (nulled below finance via field mask)
    priority_level: int
    delivery_person_id: Optional[str] = None
    delivery_person: Optional[dict] = None
    telecaller: Optional[UserResponse] = None
    assigned_outlet: Optional[OutletResponse] = None
    items: List[OrderItemResponse] = []
    created_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# TRANSACTION MODELS
# ============================================================================

class OrderTransactionCreateRequest(BaseModel):
    order_id: str
    payment_status: PaymentStatus
    payment_method: PaymentMethod
    amount_paid: Decimal = Field(..., gt=0)
    transaction_reference: Optional[str] = None
    notes: Optional[str] = None


class OrderTransactionResponse(BaseModel):
    uid: str
    order_id: str
    payment_status: PaymentStatus
    payment_method: PaymentMethod
    amount_paid: Decimal
    transaction_reference: Optional[str] = None
    payment_date: datetime
    received_by: str
    notes: Optional[str] = None

    class Config:
        from_attributes = True

class OrderTransactionUpdateRequest(BaseModel):
    payment_method: Optional[PaymentMethod] = None
    amount_paid: Optional[Decimal] = Field(None, gt=0)
    transaction_reference: Optional[str] = None
    notes: Optional[str] = None
    payment_status: PaymentStatus = Field(..., description="New payment status")
    
class PaymentStatusUpdateRequest(BaseModel):
    """Request model for updating order payment status"""
    payment_status: PaymentStatus = Field(..., description="New payment status")
    notes: Optional[str] = Field(None, description="Reason for status change")


# ============================================================================
# INVOICE MODELS
# ============================================================================

class InvoiceItemRequest(BaseModel):
    product_id: str
    quantity: int = Field(..., gt=0)
    product_manual_discount: Decimal = Field(default=0, ge=0, description="Manual discount per unit in rupees")
    discount_percentage: Optional[Decimal] = Field(None, ge=0, le=100, description="Deprecated - use product_manual_discount instead")


class InvoiceCreateRequest(BaseModel):
    outlet_id: str
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    customer_address: Optional[str] = None
    customer_gstin: Optional[str] = None
    customer_state_code: Optional[str] = None
    payment_method: PaymentMethod
    items: List[InvoiceItemRequest]
    discount_amount: Decimal = Field(default=0, ge=0, description="Deprecated - ignored in processing")
    prepaid_amount: Decimal = Field(default=0, ge=0, description="Amount paid in advance")
    notes: Optional[str] = None


class InvoiceItemResponse(BaseModel):
    uid: str
    product_id: str
    product_name: str
    hsn_code: str
    quantity: int
    unit_price: Decimal
    total_price: Decimal  # quantity * unit_price (before discount)
    product_manual_discount: Decimal  # Manual discount per unit
    discount_percentage: Optional[Decimal] = None  # Deprecated field
    discount_amount: Decimal
    taxable_amount: Decimal
    tax_rate: Decimal
    cgst_rate: Decimal
    cgst_amount: Decimal
    sgst_rate: Decimal
    sgst_amount: Decimal
    igst_rate: Decimal
    igst_amount: Decimal
    total_tax: Decimal
    total_amount: Decimal

    class Config:
        from_attributes = True


class InvoiceResponse(BaseModel):
    uid: str
    invoice_number: str
    outlet_id: str
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    customer_address: Optional[str] = None
    customer_gstin: Optional[str] = None
    customer_state_code: Optional[str] = None
    invoice_date: date
    invoice_type: InvoiceType
    payment_method: PaymentMethod
    payment_status: PaymentStatus
    subtotal: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_tax: Decimal
    total_amount: Decimal
    amount_paid: Decimal
    balance_amount: Decimal
    prepaid_amount: Decimal  # New field
    paid_at_outlet: Decimal  # New field
    notes: Optional[str] = None
    is_cancelled: bool
    cancelled_reason: Optional[str] = None
    created_by: str
    items: List[InvoiceItemResponse] = []
    created_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# STOCK TRANSFER MODELS
# ============================================================================

class TransferItemRequest(BaseModel):
    product_id: str
    quantity_requested: int = Field(..., gt=0)


class StockTransferCreateRequest(BaseModel):
    from_outlet_id: str = Field(..., min_length=1)  # source is mandatory (no NULL warehouse)
    to_outlet_id: str = Field(..., min_length=1)
    scheduled_date: Optional[date] = None
    items: List[TransferItemRequest]
    notes: Optional[str] = None

class TransferItemRequestBulk(BaseModel):
    sku: str
    quantity_requested: int = Field(..., gt=0)


class StockTransferCreateRequestBulk(BaseModel):
    from_outlet_name: str = Field(..., min_length=1)  # source is mandatory (no NULL warehouse)
    to_outlet_name: str = Field(..., min_length=1)
    scheduled_date: Optional[date] = None
    items: List[TransferItemRequestBulk]
    notes: Optional[str] = None

class BulkTransferResponse(BaseModel):
    successful_count: int
    failed_count: int
    successful_transfer_ids: List[str]
    errors: List[Dict[str, Any]]

class StockTransferStatusUpdateRequest(BaseModel):
    status: TransferStatus
    notes: Optional[str] = None


class TransferItemApproveRequest(BaseModel):
    product_id: str
    approved_quantity: int = Field(..., ge=0)


class StockTransferApproveQuantitiesRequest(BaseModel):
    items: List[TransferItemApproveRequest]
    notes: Optional[str] = None


# ProductBrief moved above OrderItemResponse


class OutletBrief(BaseModel):
    uid: str
    outlet_name: str
    outlet_code: str

    class Config:
        from_attributes = True


class UserBrief(BaseModel):
    uid: str
    full_name: str
    email: Optional[str] = None
    role: str

    class Config:
        from_attributes = True


class TransferItemResponse(BaseModel):
    uid: str
    product_id: str
    product: Optional[ProductBrief] = None
    quantity_requested: int
    quantity_delivered: int

    class Config:
        from_attributes = True


class StockTransferResponse(BaseModel):
    uid: str
    transfer_number: Optional[str] = None
    from_outlet_id: Optional[str] = None
    from_outlet: Optional[OutletBrief] = None
    to_outlet_id: str
    to_outlet: Optional[OutletBrief] = None
    status: TransferStatus
    requested_by: str
    requested_by_user: Optional[UserBrief] = None
    approved_by: Optional[str] = None
    delivery_person_id: Optional[str] = None
    scheduled_date: Optional[date] = None
    delivered_date: Optional[datetime] = None
    notes: Optional[str] = None
    items: List[TransferItemResponse] = []
    created_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# SYSTEM CONFIGURATION MODELS
# ============================================================================

class SystemConfigurationUpdateRequest(BaseModel):
    company_name: Optional[str] = None
    company_address: Optional[str] = None
    company_gstin: Optional[str] = None
    company_pan: Optional[str] = None
    company_logo_url: Optional[str] = None
    invoice_terms: Optional[str] = None
    invoice_footer: Optional[str] = None
    cgst_default_rate: Optional[Decimal] = None
    sgst_default_rate: Optional[Decimal] = None
    igst_default_rate: Optional[Decimal] = None
    price_includes_tax: Optional[bool] = None


class SystemConfigurationResponse(BaseModel):
    uid: str
    company_name: str
    company_address: str
    company_gstin: str
    company_pan: str
    company_logo_url: Optional[str] = None
    invoice_terms: Optional[str] = None
    invoice_footer: Optional[str] = None
    cgst_default_rate: Decimal
    sgst_default_rate: Decimal
    igst_default_rate: Decimal
    price_includes_tax: bool
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ============================================================================
# OUTLET DAILY COLLECTION MODELS
# ============================================================================

class OutletCollectionCreateRequest(BaseModel):
    collection_date: date = Field(..., description="Collection date")
    outlet_id: str = Field(..., description="Outlet UID")
    amount: Decimal = Field(..., gt=0, description="Amount collected")
    payment_mode: OutletPaymentMode = Field(..., description="Payment mode")
    payment_sub_mode: OutletPaymentSubMode = Field(..., description="Payment sub-mode")
    transaction_id: Optional[str] = Field(None, description="Transaction reference number")
    remarks: Optional[str] = Field(None, description="Additional notes")


class OutletCollectionStatusUpdateRequest(BaseModel):
    confirmation_status: OutletCollectionStatus = Field(..., description="New confirmation status")


class OutletCollectionResponse(BaseModel):
    uid: str
    collection_date: date
    outlet_id: str
    amount: Decimal
    payment_mode: OutletPaymentMode
    payment_sub_mode: OutletPaymentSubMode
    transaction_id: Optional[str] = None
    remarks: Optional[str] = None
    confirmation_status: OutletCollectionStatus
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class OutletCollectionSummaryResponse(BaseModel):
    """
    Summary of outlet collection figures.

    - to_be_collected: Sum of total_amount from DELIVERED orders filtered by actual_delivery_date.
    - confirmed_collections: Sum of amount from outlet-collection records with status CONFIRMED.
    - outstanding: to_be_collected minus confirmed_collections.
    """
    outlet_id: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    to_be_collected: Decimal = Decimal("0.00")
    confirmed_collections: Decimal = Decimal("0.00")
    outstanding: Decimal = Decimal("0.00")

    class Config:
        from_attributes = True


# ============================================================================
# OUTLET MANAGER PAYOUT MODELS
# ============================================================================

class PayoutCreateRequest(BaseModel):
    outlet_id: str = Field(..., description="Outlet UID")
    outlet_manager_id: str = Field(..., description="Outlet manager UID")
    period_from: date = Field(..., description="Start date of payout period")
    period_to: date = Field(..., description="End date of payout period")
    amount: Decimal = Field(..., gt=0, description="Payout amount")
    payment_method: PaymentMethod = Field(..., description="Payment method")
    payment_date: Optional[date] = Field(None, description="Actual payment date")
    transaction_id: Optional[str] = Field(None, description="Transaction reference")
    remarks: Optional[str] = Field(None, description="Additional notes")

    @validator('period_to')
    def validate_period(cls, v, values):
        if 'period_from' in values and v < values['period_from']:
            raise ValueError('period_to must be after period_from')
        return v


class PayoutUpdateRequest(BaseModel):
    amount: Optional[Decimal] = Field(None, gt=0, description="Payout amount")
    payment_method: Optional[PaymentMethod] = Field(None, description="Payment method")
    payment_date: Optional[date] = Field(None, description="Actual payment date")
    transaction_id: Optional[str] = Field(None, description="Transaction reference")
    remarks: Optional[str] = Field(None, description="Additional notes")


class PayoutStatusUpdateRequest(BaseModel):
    status: PayoutStatus = Field(..., description="New payout status")
    remarks: Optional[str] = Field(None, description="Reason for status change")


class PayoutResponse(BaseModel):
    uid: str
    outlet_id: str
    outlet_name: Optional[str] = None
    outlet_manager_id: str
    outlet_manager_name: Optional[str] = None
    period_from: date
    period_to: date
    amount: Decimal
    payment_date: Optional[date] = None
    payment_method: PaymentMethod
    transaction_id: Optional[str] = None
    status: PayoutStatus
    remarks: Optional[str] = None
    created_by: str
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    paid_by: Optional[str] = None
    paid_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MarkPayoutPaidRequest(BaseModel):
    payment_date: date = Field(..., description="Date of payment YYYY-MM-DD")
    notes: Optional[str] = Field(None, description="Optional remarks for the payment")

# ============================================================================
# DELIVERY GUY MODELS
# ============================================================================

class DeliveryGuyCreateRequest(BaseModel):
    email: str = Field(..., description="User email address")
    password: Optional[str] = None
    full_name: str
    phone: str
    outlet_id: str
    payout_frequency: PayoutFrequency = PayoutFrequency.WEEKLY

class DeliveryGuyUpdateRequest(BaseModel):
    is_active_for_delivery: Optional[bool] = None
    outlet_id: Optional[str] = None
    payout_frequency: Optional[PayoutFrequency] = None
    is_deleted: Optional[bool] = None

class DeliveryGuyResponse(BaseModel):
    uid: str
    user_id: str
    outlet_id: str
    is_active_for_delivery: bool
    payout_frequency: PayoutFrequency
    is_deleted: bool
    created_at: datetime
    user: Optional[UserResponse] = None
    outlet: Optional[OutletResponse] = None

    class Config:
        from_attributes = True

class BulkOrderDeliveryAssignmentRequest(BaseModel):
    order_ids: List[str]
    delivery_guy_id: str

class BulkAssignmentResult(BaseModel):
    order_id: str
    status: str
    message: Optional[str] = None

class BulkAssignmentResponse(BaseModel):
    successful_count: int
    failed_count: int
    results: List[BulkAssignmentResult]


# ============================================================================
# DELIVERY GUY HANDOVER MODELS
# ============================================================================

class DeliveryHandoverCreateRequest(BaseModel):
    delivery_guy_id: str = Field(..., description="Delivery Guy User UID")
    outlet_id: str = Field(..., description="Outlet UID")
    amount: Decimal = Field(..., gt=0, description="Amount handed over")
    handover_date: date = Field(..., description="Date of handover")
    remarks: Optional[str] = Field(None, description="Additional notes")


class DeliveryHandoverStatusUpdateRequest(BaseModel):
    status: OutletCollectionStatus = Field(..., description="New confirmation status")


class DeliveryHandoverResponse(BaseModel):
    uid: str
    delivery_guy_id: str
    outlet_id: str
    amount: Decimal
    handover_date: date
    status: OutletCollectionStatus
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    remarks: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    # Optional nested details
    delivery_guy: Optional[UserResponse] = None
    outlet: Optional[OutletResponse] = None

    class Config:
        from_attributes = True


class DeliveryGuyCashBalanceResponse(BaseModel):
    delivery_guy_id: str
    current_cash_balance: Decimal
    total_collected: Decimal
    total_handed_over: Decimal


# ============================================================================
# RIDER RATE CARD MODELS
# ============================================================================

class RateCardCreateRequest(BaseModel):
    outlet_id: str
    pay_per_order: Decimal = Field(..., gt=0)
    is_active: bool = True

class RateCardUpdateRequest(BaseModel):
    pay_per_order: Optional[Decimal] = Field(None, gt=0)
    is_active: Optional[bool] = None

class RateCardResponse(BaseModel):
    uid: str
    outlet_id: str
    pay_per_order: Decimal
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None
    outlet: Optional[OutletResponse] = None

    class Config:
        from_attributes = True


# ============================================================================
# RIDER PAYOUT MODELS
# ============================================================================

class RiderPayoutCreateRequest(BaseModel):
    rider_id: str
    outlet_id: str
    period_from: date
    period_to: date
    payment_method: Optional[PaymentMethod] = None
    transaction_id: Optional[str] = None
    remarks: Optional[str] = None

    @validator('period_to')
    def validate_period(cls, v, values):
        if 'period_from' in values and v < values['period_from']:
            raise ValueError('period_to must be after period_from')
        return v

class RiderPayoutUpdateRequest(BaseModel):
    status: Optional[PayoutStatus] = None
    payment_method: Optional[PaymentMethod] = None
    transaction_id: Optional[str] = None
    payment_date: Optional[date] = None
    remarks: Optional[str] = None

class RiderPayoutStatusUpdateRequest(BaseModel):
    status: PayoutStatus
    remarks: Optional[str] = None

class RiderPayoutResponse(BaseModel):
    uid: str
    rider_id: str
    outlet_id: str
    period_from: date
    period_to: date
    total_amount: Decimal
    status: PayoutStatus
    payment_method: Optional[PaymentMethod] = None
    transaction_id: Optional[str] = None
    payment_date: Optional[date] = None
    remarks: Optional[str] = None
    created_by: str
    paid_by: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    # Optional nested details
    rider: Optional[UserResponse] = None
    outlet: Optional[OutletResponse] = None

    class Config:
        from_attributes = True


class RiderPayoutSummaryItem(BaseModel):
    rider_id: str
    full_name: str
    phone: str
    outlet_id: str
    outlet_name: str
    payout_frequency: PayoutFrequency
    pending_amount: Decimal
    pending_order_count: int
    last_payout_date: Optional[date] = None
    last_payout_amount: Optional[Decimal] = None


# ============================================================================
# SMARTPING DRIP MODELS
# ============================================================================

class SmartpingCampaignRegistryBaseRequest(BaseModel):
    event_key: str
    business_event_key: str
    campaign_name: str
    provider: str = "smartping"
    is_active: bool = True
    version: int = 1
    trigger_delay_value: int = 0
    trigger_delay_unit: str = "minutes"
    template_param_keys: List[str] = Field(default_factory=list)
    media: Optional[Dict[str, Any]] = None
    buttons: Optional[List[Dict[str, Any]]] = None
    attributes: Optional[Dict[str, str]] = None
    tags: Optional[List[str]] = None
    params_fallback_value: Optional[Dict[str, str]] = None
    source: Optional[str] = None
    notes: Optional[str] = None


class SmartpingCampaignRegistryCreateRequest(SmartpingCampaignRegistryBaseRequest):
    pass


class SmartpingCampaignRegistryUpdateRequest(BaseModel):
    business_event_key: Optional[str] = None
    campaign_name: Optional[str] = None
    provider: Optional[str] = None
    is_active: Optional[bool] = None
    version: Optional[int] = None
    trigger_delay_value: Optional[int] = None
    trigger_delay_unit: Optional[str] = None
    template_param_keys: Optional[List[str]] = None
    media: Optional[Dict[str, Any]] = None
    buttons: Optional[List[Dict[str, Any]]] = None
    attributes: Optional[Dict[str, str]] = None
    tags: Optional[List[str]] = None
    params_fallback_value: Optional[Dict[str, str]] = None
    source: Optional[str] = None
    notes: Optional[str] = None


class SmartpingCampaignRegistryResponse(SmartpingCampaignRegistryBaseRequest):
    uid: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SmartpingEventEnqueueRequest(BaseModel):
    business_event_ref: str = Field(..., description="Stable reference for the business event, e.g. cart/order id")
    destination: str
    user_name: str
    context: Dict[str, Any] = Field(default_factory=dict)
    max_retries: int = 3
    send_at: Optional[datetime] = Field(
        default=None,
        description="Optional override for the first dispatch time. If omitted, registry delay is used.",
    )


class SmartpingJobResponse(BaseModel):
    uid: str
    event_key: str
    business_event_key: str
    business_event_ref: str
    registry_uid: str
    destination: str
    user_name: str
    send_at: datetime
    status: str
    retry_count: int
    max_retries: int
    idempotency_key: str
    context_payload: Optional[Dict[str, Any]] = None
    request_payload: Optional[Dict[str, Any]] = None
    response_payload: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    locked_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SmartpingDispatchResponse(BaseModel):
    claimed: int
    sent: int
    failed: int
    retried: int
    skipped: int = 0
    details: List[Dict[str, Any]] = Field(default_factory=list)


class SmartpingEnqueueResponse(BaseModel):
    created: int
    skipped: int = 0
    jobs: List[SmartpingJobResponse] = Field(default_factory=list)


# ============================================================================
# ROLES ADMIN (superadmin-editable roles & permissions)
# ============================================================================

class RoleCreateRequest(BaseModel):
    name: str
    scope_level: str
    location_type: Optional[str] = None
    perms: List[str] = Field(default_factory=list)


class RolePermsUpdateRequest(BaseModel):
    perms: List[str]


class RoleItemResponse(BaseModel):
    name: str
    scope_level: str
    location_type: Optional[str] = None
    is_system: bool
    perms: List[str]


class RolesCatalogResponse(BaseModel):
    roles: List[RoleItemResponse]
    permissions: List[str]

# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    # Agency
    "AgencyCreateRequest", "AgencyResponse",

    # User
    "UserCreateRequest", "UserUpdateRequest", "UserPasswordChangeRequest", "UserResponse",
    
    # Outlet
    "OutletCreateRequest", "OutletUpdateRequest", "OutletResponse",
    
    # Product
    "ProductCategoryCreateRequest", "ProductCategoryResponse",
    "ProductCreateRequest", "ProductUpdateRequest", "ProductResponse",
    
    # Inventory
    "InventoryResponse", "StockAdjustmentRequest","InventoryAuditResponse",
    
    # Order
    "OrderItemRequest", "OrderPaymentRequest", "OrderCreateRequest", "ProxyOrderCreateRequest", "OrderUpdateRequest",
    "OrderFullUpdateRequest",
    "OrderStatusUpdateRequest", "OrderAssignRequest", "OrderRevokeRequest",
    "OrderItemResponse", "OrderResponse",
    
    # Transaction
    "OrderTransactionCreateRequest", "OrderTransactionResponse",
    "PaymentStatusUpdateRequest","OrderTransactionUpdateRequest",
    
    # Invoice
    "InvoiceItemRequest", "InvoiceCreateRequest",
    "InvoiceItemResponse", "InvoiceResponse",
    
    # Stock Transfer
    "TransferItemRequest", "StockTransferCreateRequest",
    "StockTransferStatusUpdateRequest", "TransferItemApproveRequest", "StockTransferApproveQuantitiesRequest",
    "TransferItemResponse", "StockTransferResponse","BulkTransferResponse" , "StockTransferCreateRequestBulk", "ProductBrief", "OutletBrief", "UserBrief",
    
    # System
    "SystemConfigurationUpdateRequest", "SystemConfigurationResponse",
    
    # Outlet Collections
    "OutletCollectionCreateRequest", "OutletCollectionStatusUpdateRequest",
    "OutletCollectionResponse", "OutletCollectionSummaryResponse",
    
    # Outlet Manager Payouts
    "PayoutCreateRequest", "PayoutUpdateRequest", "PayoutStatusUpdateRequest", "PayoutResponse", "MarkPayoutPaidRequest",
    
    # Delivery Guy
    "DeliveryGuyCreateRequest", "DeliveryGuyUpdateRequest", "DeliveryGuyResponse",
    "BulkOrderDeliveryAssignmentRequest", "BulkAssignmentResult", "BulkAssignmentResponse",
    
    # Delivery Guy Handovers
    "DeliveryHandoverCreateRequest", "DeliveryHandoverStatusUpdateRequest", 
    "DeliveryHandoverResponse", "DeliveryGuyCashBalanceResponse",

    # Rider Payouts & Rate Cards
    "RateCardCreateRequest", "RateCardUpdateRequest", "RateCardResponse",
    "RiderPayoutCreateRequest", "RiderPayoutUpdateRequest", "RiderPayoutStatusUpdateRequest", "RiderPayoutResponse", "RiderPayoutSummaryItem",

    # SmartPing Drip
    "SmartpingCampaignRegistryBaseRequest", "SmartpingCampaignRegistryCreateRequest", "SmartpingCampaignRegistryUpdateRequest",
    "SmartpingCampaignRegistryResponse",
    "SmartpingEventEnqueueRequest",
    "SmartpingJobResponse",
    "SmartpingDispatchResponse",
    "SmartpingEnqueueResponse",

    # Roles admin
    "RoleCreateRequest", "RolePermsUpdateRequest", "RoleItemResponse", "RolesCatalogResponse",
]
