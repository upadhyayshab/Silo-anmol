from pydantic import BaseModel, Field, validator
from typing import Optional, List
from datetime import datetime, date
from decimal import Decimal
import re

from utils.constants import (
    UserRole, OrderStatus, CollectionType, PaymentMethod,
    PaymentStatus, InvoiceType, TransferStatus, UnitOfMeasure,
    OutletPaymentMode, OutletPaymentSubMode, OutletCollectionStatus
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


class UserPasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8)


class UserResponse(BaseModel):
    uid: str
    email: str
    full_name: str
    role: UserRole
    phone: Optional[str]
    outlet_id: Optional[str]
    is_active: bool
    last_login: Optional[datetime]
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


# ============================================================================
# OUTLET MODELS
# ============================================================================

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
    manager_id: Optional[str] = None


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
    manager_id: Optional[str] = None
    is_active: Optional[bool] = None


class OutletResponse(BaseModel):
    uid: str
    outlet_name: str
    outlet_code: str
    address: str
    city: str
    state: str
    pincode: str
    phone: str
    email: Optional[str]
    gstin: str
    state_code: str
    pan: str
    manager_id: Optional[str]
    is_active: bool
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
    description: Optional[str]
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
    commission: Decimal = Field(default=0.00, ge=0, description="Commission in rupees")
    discount: Decimal = Field(default=0.00, ge=0, description="Discount in rupees")


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
    commission: Optional[Decimal] = Field(None, ge=0, description="Commission in rupees")
    discount: Optional[Decimal] = Field(None, ge=0, description="Discount in rupees")
    is_active: Optional[bool] = None


class ProductResponse(BaseModel):
    uid: str
    sku: str
    product_name: str
    description: Optional[str]
    category_id: str
    hsn_code: str
    tax_rate: Decimal
    unit_price: Decimal
    cost_price: Decimal
    unit_of_measure: UnitOfMeasure
    barcode: Optional[str]
    image_url: Optional[str]
    min_stock_level: int
    commission: Decimal
    discount: Decimal
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
    outlet_id: Optional[str]
    quantity: int
    reserved_quantity: int
    available_quantity: int  # Computed: quantity - reserved_quantity
    last_updated: datetime

    class Config:
        from_attributes = True


class StockAdjustmentRequest(BaseModel):
    product_id: str
    outlet_id: Optional[str] = None  # NULL for warehouse
    quantity_change: int  # Positive for addition, negative for reduction
    reason: str


# ============================================================================
# ORDER MODELS
# ============================================================================

class OrderItemRequest(BaseModel):
    product_id: str
    quantity: int = Field(..., gt=0)
    product_manual_discount: Decimal = Field(default=Decimal('0.00'), ge=0, description="Manual discount for this product in rupees")
    # unit_price removed - now calculated automatically from product.cost_price - product_manual_discount


class OrderCreateRequest(BaseModel):
    customer_name: str
    customer_phone: str
    house_no: Optional[str] = None
    street: Optional[str] = None
    address_line: str
    village: Optional[str] = None
    taluk: Optional[str] = None
    district: str
    state: str
    pincode: str
    collection_type: CollectionType
    payment_method: PaymentMethod
    expected_delivery_date: Optional[date] = None
    manual_discount: Decimal = Field(default=Decimal('0.00'), ge=0, description="Manual discount in rupees for entire order")
    prepaid_amount: Decimal = Field(default=Decimal('0.00'), ge=0, description="Amount already paid in advance")
    items: List[OrderItemRequest]


class OrderUpdateRequest(BaseModel):
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    address_line: Optional[str] = None
    district: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    expected_delivery_date: Optional[date] = None


class OrderStatusUpdateRequest(BaseModel):
    order_status: OrderStatus
    status_remarks: Optional[str] = None  # Mandatory for cancelled


class OrderAssignRequest(BaseModel):
    assigned_outlet_id: str


class OrderItemResponse(BaseModel):
    uid: str
    product_id: str
    quantity: int
    unit_price: Decimal
    subtotal: Decimal
    product_manual_discount: Decimal

    class Config:
        from_attributes = True


class OrderResponse(BaseModel):
    uid: str
    order_number: str
    customer_name: str
    customer_phone: str
    house_no: Optional[str]
    street: Optional[str]
    address_line: str
    village: Optional[str]
    taluk: Optional[str]
    district: str
    state: str
    pincode: str
    telecaller_id: str
    assigned_outlet_id: Optional[str]
    order_status: OrderStatus
    collection_type: CollectionType
    payment_method: PaymentMethod
    order_date: datetime
    expected_delivery_date: Optional[date]
    actual_delivery_date: Optional[datetime]
    status_remarks: Optional[str]
    gross_amount: Decimal  # Total before manual discount
    manual_discount: Decimal  # Manual discount applied (legacy field, kept for compatibility)
    discount_applied: Decimal  # Total discount (product manual discounts)
    prepaid_amount: Decimal  # Amount already paid
    total_amount: Decimal  # Final net amount
    total_commission: Decimal  # Total commission for the order
    items: List[OrderItemResponse] = []
    created_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# TRANSACTION MODELS
# ============================================================================

class OrderTransactionCreateRequest(BaseModel):
    order_id: str
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
    transaction_reference: Optional[str]
    payment_date: datetime
    received_by: str
    notes: Optional[str]

    class Config:
        from_attributes = True


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
    customer_name: Optional[str]
    customer_phone: Optional[str]
    customer_email: Optional[str]
    customer_address: Optional[str]
    customer_gstin: Optional[str]
    customer_state_code: Optional[str]
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
    notes: Optional[str]
    is_cancelled: bool
    cancelled_reason: Optional[str]
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
    from_outlet_id: Optional[str] = None  # NULL for warehouse
    to_outlet_id: str
    scheduled_date: Optional[date] = None
    items: List[TransferItemRequest]
    notes: Optional[str] = None


class StockTransferStatusUpdateRequest(BaseModel):
    status: TransferStatus
    notes: Optional[str] = None


class TransferItemResponse(BaseModel):
    uid: str
    product_id: str
    quantity_requested: int
    quantity_delivered: int

    class Config:
        from_attributes = True


class StockTransferResponse(BaseModel):
    uid: str
    from_outlet_id: Optional[str]
    to_outlet_id: str
    status: TransferStatus
    requested_by: str
    approved_by: Optional[str]
    delivery_person_id: Optional[str]
    scheduled_date: Optional[date]
    delivered_date: Optional[datetime]
    notes: Optional[str]
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
    company_logo_url: Optional[str]
    invoice_terms: Optional[str]
    invoice_footer: Optional[str]
    cgst_default_rate: Decimal
    sgst_default_rate: Decimal
    igst_default_rate: Decimal
    price_includes_tax: bool
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


# ============================================================================
# OUTLET DAILY COLLECTION MODELS
# ============================================================================

class OutletCollectionCreateRequest(BaseModel):
    date: date = Field(..., description="Collection date")
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
    date: date
    outlet_id: str
    amount: Decimal
    payment_mode: OutletPaymentMode
    payment_sub_mode: OutletPaymentSubMode
    transaction_id: Optional[str]
    remarks: Optional[str]
    confirmation_status: OutletCollectionStatus
    confirmed_by: Optional[str]
    confirmed_at: Optional[datetime]
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    # User
    "UserCreateRequest", "UserUpdateRequest", "UserPasswordChangeRequest", "UserResponse",
    
    # Outlet
    "OutletCreateRequest", "OutletUpdateRequest", "OutletResponse",
    
    # Product
    "ProductCategoryCreateRequest", "ProductCategoryResponse",
    "ProductCreateRequest", "ProductUpdateRequest", "ProductResponse",
    
    # Inventory
    "InventoryResponse", "StockAdjustmentRequest",
    
    # Order
    "OrderItemRequest", "OrderCreateRequest", "OrderUpdateRequest",
    "OrderStatusUpdateRequest", "OrderAssignRequest",
    "OrderItemResponse", "OrderResponse",
    
    # Transaction
    "OrderTransactionCreateRequest", "OrderTransactionResponse",
    "PaymentStatusUpdateRequest",
    
    # Invoice
    "InvoiceItemRequest", "InvoiceCreateRequest",
    "InvoiceItemResponse", "InvoiceResponse",
    
    # Stock Transfer
    "TransferItemRequest", "StockTransferCreateRequest",
    "StockTransferStatusUpdateRequest",
    "TransferItemResponse", "StockTransferResponse",
    
    # System
    "SystemConfigurationUpdateRequest", "SystemConfigurationResponse",
    
    # Outlet Collections
    "OutletCollectionCreateRequest", "OutletCollectionStatusUpdateRequest",
    "OutletCollectionResponse",
]
