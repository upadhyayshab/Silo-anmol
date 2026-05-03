import sqlalchemy as db
from sqlalchemy.orm import relationship
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List

from SharedBackend.managers import BaseSchema, GenericManager, BasePassSchema, BasePassManager
from SharedBackend.managers.base import NESTED_JOINS
from utils.constants import (
    UserRole, OrderStatus, CollectionType, PaymentMethod, 
    PaymentStatus, InvoiceType, TransferStatus, UnitOfMeasure,
    OutletPaymentMode, OutletPaymentSubMode, OutletCollectionStatus,
    PayoutStatus
)


# ============================================================================
# USER MANAGEMENT
# ============================================================================

class UserSchema(BasePassSchema):
    """User accounts with role-based access"""
    __tablename__ = "users"

    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(255), nullable=False)
    role = db.Column(db.Enum(UserRole), nullable=False, index=True)
    phone = db.Column(db.String(20))
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    last_login = db.Column(db.DateTime(timezone=True), nullable=True)
    
    # Password reset fields
    password_reset_token = db.Column(db.String(255), nullable=True, index=True)
    password_reset_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Relationships
    outlet = relationship("OutletSchema", back_populates="users", foreign_keys=[outlet_id])
    created_orders = relationship("CustomerOrderSchema", back_populates="telecaller", foreign_keys="CustomerOrderSchema.telecaller_id")
    received_transactions = relationship("OrderTransactionSchema", back_populates="receiver", foreign_keys="OrderTransactionSchema.received_by")
    created_sales = relationship("SalesTransactionSchema", back_populates="creator", foreign_keys="SalesTransactionSchema.created_by")
    created_invoices = relationship("SalesInvoiceSchema", back_populates="creator", foreign_keys="SalesInvoiceSchema.created_by")
    requested_transfers = relationship("StockTransferOrderSchema", back_populates="requester", foreign_keys="StockTransferOrderSchema.requested_by")
    approved_transfers = relationship("StockTransferOrderSchema", back_populates="approver", foreign_keys="StockTransferOrderSchema.approved_by")
    delivery_transfers = relationship("StockTransferOrderSchema", back_populates="delivery_person", foreign_keys="StockTransferOrderSchema.delivery_person_id")
    activity_logs = relationship("ActivityLogSchema", back_populates="user")
    notifications = relationship("NotificationSchema", back_populates="user")


class UserManager(BasePassManager[UserSchema]):
    async def update(
            self,
            uid: str,
            updates: dict,
            *,
            session: AsyncSession = None,
            joins: List[NESTED_JOINS] = None,
            include: List[str] = None,
            exclude: List[str] = None,
    ) -> UserSchema:
        """
        Override update method to handle session management properly
        and avoid detached instance errors
        """
        # Always fetch fresh data after update to avoid session issues
        await super().update(uid, updates, session=session, joins=joins, include=include, exclude=exclude)
        return await self.fetch(uid, session=session, joins=joins, include=include, exclude=exclude)


# ============================================================================
# OUTLET MANAGEMENT
# ============================================================================

class OutletSchema(BaseSchema):
    """Franchise outlet locations with GST details"""
    __tablename__ = "outlets"

    outlet_name = db.Column(db.String(255), nullable=False)
    outlet_code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    address = db.Column(db.Text, nullable=False)
    city = db.Column(db.String(100), nullable=False)
    state = db.Column(db.String(100), nullable=False)
    pincode = db.Column(db.String(10), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(255))
    gstin = db.Column(db.String(15), nullable=False)
    state_code = db.Column(db.String(2), nullable=False)
    pan = db.Column(db.String(10), nullable=False)
    lat_lon = db.Column(db.JSON, nullable=True) # [lat, lon]
    manager_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Relationships
    manager = relationship("UserSchema", foreign_keys=[manager_id])
    users = relationship("UserSchema", back_populates="outlet", foreign_keys="UserSchema.outlet_id")
    inventory = relationship("InventorySchema", back_populates="outlet")
    invoices = relationship("SalesInvoiceSchema", back_populates="outlet")
    orders = relationship("CustomerOrderSchema", back_populates="assigned_outlet")
    sales = relationship("SalesTransactionSchema", back_populates="outlet")
    transfer_from = relationship("StockTransferOrderSchema", back_populates="from_outlet", foreign_keys="StockTransferOrderSchema.from_outlet_id")
    transfer_to = relationship("StockTransferOrderSchema", back_populates="to_outlet", foreign_keys="StockTransferOrderSchema.to_outlet_id")
    delivery_tracking = relationship("DeliveryTrackingSchema", back_populates="outlet")


class OutletManager(GenericManager[OutletSchema]):
    pass


# ============================================================================
# PRODUCT MANAGEMENT
# ============================================================================

class ProductCategorySchema(BaseSchema):
    """Product categories for organization"""
    __tablename__ = "product_categories"

    category_name = db.Column(db.String(255), nullable=False, unique=True)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Relationships
    products = relationship("ProductSchema", back_populates="category")


class ProductCategoryManager(GenericManager[ProductCategorySchema]):
    pass


class ProductSchema(BaseSchema):
    """Products with GST and inventory details"""
    __tablename__ = "products"

    sku = db.Column(db.String(100), unique=True, nullable=False, index=True)
    product_name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    category_id = db.Column(db.String, db.ForeignKey("product_categories.uid"), nullable=False)
    hsn_code = db.Column(db.String(8), nullable=False)
    tax_rate = db.Column(db.Numeric(5, 2), nullable=False)  # e.g., 18.00 for 18%
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    cost_price = db.Column(db.Numeric(10, 2), nullable=False)
    unit_of_measure = db.Column(db.Enum(UnitOfMeasure), nullable=False)
    barcode = db.Column(db.String(100), unique=True, nullable=True, index=True)
    image_url = db.Column(db.String(500))
    min_stock_level = db.Column(db.Integer, default=10, nullable=False)
    lsq_display_name = db.Column(db.String(255), nullable=True)
    # New fields for commission and discount (in rupees)
    commission = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)  # Commission in rupees
    discount = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)    # Discount in rupees
    margin = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)      # Margin for non-Silo Fortune products (in rupees)
    
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Relationships
    category = relationship("ProductCategorySchema", back_populates="products")
    inventory = relationship("InventorySchema", back_populates="product")
    order_items = relationship("OrderItemSchema", back_populates="product")
    invoice_items = relationship("SalesInvoiceItemSchema", back_populates="product")
    sales = relationship("SalesTransactionSchema", back_populates="product")
    transfer_items = relationship("TransferItemSchema", back_populates="product")


class ProductManager(GenericManager[ProductSchema]):
    pass


# ============================================================================
# INVENTORY MANAGEMENT
# ============================================================================

class InventorySchema(BaseSchema):
    """Multi-location inventory tracking"""
    __tablename__ = "inventory"
    __table_args__ = (
        db.UniqueConstraint('product_id', 'outlet_id', name='uq_product_outlet'),
    )

    product_id = db.Column(db.String, db.ForeignKey("products.uid"), nullable=False, index=True)
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=True, index=True)  # NULL = warehouse
    quantity = db.Column(db.Integer, default=0, nullable=False)
    reserved_quantity = db.Column(db.Integer, default=0, nullable=False)
    last_updated = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    product = relationship("ProductSchema", back_populates="inventory")
    outlet = relationship("OutletSchema", back_populates="inventory")


class InventoryManager(GenericManager[InventorySchema]):
    pass


# ============================================================================
# INVOICE MANAGEMENT (Outlet Walk-in Sales)
# ============================================================================

class SalesInvoiceSchema(BaseSchema):
    """GST-compliant invoices for outlet sales"""
    __tablename__ = "sales_invoices"

    invoice_number = db.Column(db.String(50), unique=True, nullable=False, index=True)
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, index=True)
    
    # Customer details (optional for quick sales)
    customer_name = db.Column(db.String(255))
    customer_phone = db.Column(db.String(20), index=True)
    customer_email = db.Column(db.String(255))
    customer_address = db.Column(db.Text)
    customer_gstin = db.Column(db.String(15))
    customer_state_code = db.Column(db.String(2))
    
    invoice_date = db.Column(db.Date, nullable=False, default=datetime.utcnow, index=True)
    invoice_type = db.Column(db.Enum(InvoiceType), default=InvoiceType.REGULAR, nullable=False)
    payment_method = db.Column(db.Enum(PaymentMethod), nullable=False)
    payment_status = db.Column(db.Enum(PaymentStatus), default=PaymentStatus.PAID, nullable=False)
    
    # Financial details
    subtotal = db.Column(db.Numeric(10, 2), nullable=False)
    discount_amount = db.Column(db.Numeric(10, 2), default=0)
    taxable_amount = db.Column(db.Numeric(10, 2), nullable=False)
    cgst_amount = db.Column(db.Numeric(10, 2), default=0)
    sgst_amount = db.Column(db.Numeric(10, 2), default=0)
    igst_amount = db.Column(db.Numeric(10, 2), default=0)
    total_tax = db.Column(db.Numeric(10, 2), nullable=False)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)
    amount_paid = db.Column(db.Numeric(10, 2), nullable=False)
    balance_amount = db.Column(db.Numeric(10, 2), default=0)
    
    # New payment tracking fields
    prepaid_amount = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)  # Amount paid in advance
    paid_at_outlet = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)  # Amount paid at outlet
    
    notes = db.Column(db.Text)
    is_cancelled = db.Column(db.Boolean, default=False, nullable=False)
    cancelled_reason = db.Column(db.Text)
    created_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False)

    # Relationships
    outlet = relationship("OutletSchema", back_populates="invoices")
    creator = relationship("UserSchema", back_populates="created_invoices")
    items = relationship("SalesInvoiceItemSchema", back_populates="invoice", cascade="all, delete-orphan")


class SalesInvoiceManager(GenericManager[SalesInvoiceSchema]):
    pass


class SalesInvoiceItemSchema(BaseSchema):
    """Line items for invoices with tax breakdown"""
    __tablename__ = "sales_invoice_items"

    invoice_id = db.Column(db.String, db.ForeignKey("sales_invoices.uid"), nullable=False, index=True)
    product_id = db.Column(db.String, db.ForeignKey("products.uid"), nullable=False)
    product_name = db.Column(db.String(255), nullable=False)  # Snapshot at sale time
    hsn_code = db.Column(db.String(8), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    total_price = db.Column(db.Numeric(10, 2), nullable=False)  # quantity * unit_price (before discount)
    product_manual_discount = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)  # New field: manual discount per unit
    discount_percentage = db.Column(db.Numeric(5, 2), default=0, nullable=True)  # Made optional for backward compatibility
    discount_amount = db.Column(db.Numeric(10, 2), default=0)
    taxable_amount = db.Column(db.Numeric(10, 2), nullable=False)
    tax_rate = db.Column(db.Numeric(5, 2), nullable=False)
    cgst_rate = db.Column(db.Numeric(5, 2), default=0)
    cgst_amount = db.Column(db.Numeric(10, 2), default=0)
    sgst_rate = db.Column(db.Numeric(5, 2), default=0)
    sgst_amount = db.Column(db.Numeric(10, 2), default=0)
    igst_rate = db.Column(db.Numeric(5, 2), default=0)
    igst_amount = db.Column(db.Numeric(10, 2), default=0)
    total_tax = db.Column(db.Numeric(10, 2), nullable=False)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)

    # Relationships
    invoice = relationship("SalesInvoiceSchema", back_populates="items")
    product = relationship("ProductSchema", back_populates="invoice_items")


class SalesInvoiceItemManager(GenericManager[SalesInvoiceItemSchema]):
    pass


# ============================================================================
# ORDER MANAGEMENT (Telecaller Orders)
# ============================================================================

class CustomerOrderSchema(BaseSchema):
    """Customer orders placed by telecallers"""
    __tablename__ = "customer_orders"

    order_number = db.Column(db.String(50), unique=True, nullable=False, index=True)
    
    # Customer details
    customer_name = db.Column(db.String(255), nullable=False)
    customer_phone = db.Column(db.String(20), nullable=False, index=True)
    house_no = db.Column(db.String(50))
    street = db.Column(db.String(255))
    address_line = db.Column(db.Text, nullable=False)
    village = db.Column(db.String(100))
    post = db.Column(db.String(100), nullable=True)
    hobli = db.Column(db.String(100), nullable=True)
    taluk = db.Column(db.String(100))
    district = db.Column(db.String(100), nullable=False)
    state = db.Column(db.String(100), nullable=False)
    pincode = db.Column(db.String(10), nullable=False)
    lat_lon = db.Column(db.JSON, nullable=True) # [lat, lon]
    
    # Order management
    telecaller_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False, index=True)
    assigned_outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=True, index=True)
    order_status = db.Column(db.Enum(OrderStatus), default=OrderStatus.PENDING, nullable=False, index=True)
    collection_type = db.Column(db.Enum(CollectionType), nullable=False)
    payment_method = db.Column(db.Enum(PaymentMethod), nullable=False)
    
    # Dates
    order_date = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)
    expected_delivery_date = db.Column(db.Date, nullable=True)
    actual_delivery_date = db.Column(db.DateTime(timezone=True), nullable=True)
    
    status_remarks = db.Column(db.Text)  # Mandatory for cancelled/pending
    
    # Pricing fields
    gross_amount = db.Column(db.Numeric(10, 2), nullable=False)  # Total before manual discount
    manual_discount = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)  # Manual discount in rupees (legacy)
    discount_applied = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)  # Total discount applied
    prepaid_amount = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)  # Amount already paid
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)  # Final net amount
    total_commission = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)  # Total commission for order
    delivery_person_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True, index=True)
    priority_level = db.Column(db.Integer, default=0, nullable=False, index=True)

    # Relationships
    telecaller = relationship("UserSchema", back_populates="created_orders", foreign_keys=[telecaller_id]) # here we will get the telecallers and the outlet manager
    assigned_outlet = relationship("OutletSchema", back_populates="orders")
    delivery_person = relationship("UserSchema", foreign_keys=[delivery_person_id])
    items = relationship("OrderItemSchema", back_populates="order", cascade="all, delete-orphan")
    transactions = relationship("OrderTransactionSchema", back_populates="order", cascade="all, delete-orphan")
    delivery_tracking = relationship("DeliveryTrackingSchema", back_populates="order", cascade="all, delete-orphan")


class CustomerOrderManager(GenericManager[CustomerOrderSchema]):
    pass


class OrderItemSchema(BaseSchema):
    """Items in customer orders"""
    __tablename__ = "order_items"

    order_id = db.Column(db.String, db.ForeignKey("customer_orders.uid"), nullable=False, index=True)
    product_id = db.Column(db.String, db.ForeignKey("products.uid"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    total_price = db.Column(db.Numeric(10, 2), nullable=False)  # Match database schema
    tax_rate = db.Column(db.Numeric(5, 2), default=0.00)
    tax_amount = db.Column(db.Numeric(10, 2), default=0.00)
    discount_percentage = db.Column(db.Numeric(5, 2), default=0.00)
    discount_amount = db.Column(db.Numeric(10, 2), default=0.00)
    subtotal = db.Column(db.Numeric(10, 2), nullable=False)
    product_manual_discount = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)  # New field

    # Relationships
    order = relationship("CustomerOrderSchema", back_populates="items")
    product = relationship("ProductSchema", back_populates="order_items")


class OrderItemManager(GenericManager[OrderItemSchema]):
    pass


class OrderTransactionSchema(BaseSchema):
    """Payment transactions for orders"""
    __tablename__ = "order_transactions"

    order_id = db.Column(db.String, db.ForeignKey("customer_orders.uid"), nullable=False, index=True)
    payment_status = db.Column(db.Enum(PaymentStatus), default=PaymentStatus.PENDING, nullable=False)
    payment_method = db.Column(db.Enum(PaymentMethod), nullable=False)
    amount_paid = db.Column(db.Numeric(10, 2), nullable=False)
    transaction_reference = db.Column(db.String(255))
    payment_date = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    received_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False)
    notes = db.Column(db.Text)

    # Relationships
    order = relationship("CustomerOrderSchema", back_populates="transactions")
    receiver = relationship("UserSchema", back_populates="received_transactions")


class OrderTransactionManager(GenericManager[OrderTransactionSchema]):
    pass


# ============================================================================
# SALES TRANSACTIONS (Quick Sales - Backward Compatibility)
# ============================================================================

class SalesTransactionSchema(BaseSchema):
    """Quick sales transactions (non-invoice based)"""
    __tablename__ = "sales_transactions"

    invoice_id = db.Column(db.String, db.ForeignKey("sales_invoices.uid"), nullable=True)  # Link to invoice if exists
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, index=True)
    product_id = db.Column(db.String, db.ForeignKey("products.uid"), nullable=False, index=True)
    quantity_sold = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)
    sale_date = db.Column(db.Date, default=datetime.utcnow, nullable=False, index=True)
    created_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False)

    # Relationships
    outlet = relationship("OutletSchema", back_populates="sales")
    product = relationship("ProductSchema", back_populates="sales")
    creator = relationship("UserSchema", back_populates="created_sales")


class SalesTransactionManager(GenericManager[SalesTransactionSchema]):
    pass


# ============================================================================
# STOCK TRANSFER MANAGEMENT
# ============================================================================

class StockTransferOrderSchema(BaseSchema):
    """Stock transfer between warehouse and outlets"""
    __tablename__ = "stock_transfer_orders"

    transfer_number = db.Column(db.String(50), unique=True, nullable=True, index=True)
    from_outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=True)  # NULL = warehouse
    to_outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, index=True)
    status = db.Column(db.Enum(TransferStatus), default=TransferStatus.PENDING, nullable=False, index=True)
    requested_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False)
    approved_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True)
    delivery_person_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True)
    scheduled_date = db.Column(db.Date, nullable=True)
    delivered_date = db.Column(db.DateTime(timezone=True), nullable=True)
    notes = db.Column(db.Text)

    # Relationships
    from_outlet = relationship("OutletSchema", back_populates="transfer_from", foreign_keys=[from_outlet_id])
    to_outlet = relationship("OutletSchema", back_populates="transfer_to", foreign_keys=[to_outlet_id])
    requester = relationship("UserSchema", back_populates="requested_transfers", foreign_keys=[requested_by])
    approver = relationship("UserSchema", back_populates="approved_transfers", foreign_keys=[approved_by])
    delivery_person = relationship("UserSchema", back_populates="delivery_transfers", foreign_keys=[delivery_person_id])
    items = relationship("TransferItemSchema", back_populates="transfer", cascade="all, delete-orphan")


class StockTransferOrderManager(GenericManager[StockTransferOrderSchema]):
    pass


class TransferItemSchema(BaseSchema):
    """Items in stock transfer orders"""
    __tablename__ = "transfer_items"

    transfer_id = db.Column(db.String, db.ForeignKey("stock_transfer_orders.uid"), nullable=False, index=True)
    product_id = db.Column(db.String, db.ForeignKey("products.uid"), nullable=False)
    quantity_requested = db.Column(db.Integer, nullable=False)
    quantity_delivered = db.Column(db.Integer, default=0, nullable=False)

    # Relationships
    transfer = relationship("StockTransferOrderSchema", back_populates="items")
    product = relationship("ProductSchema", back_populates="transfer_items")


class TransferItemManager(GenericManager[TransferItemSchema]):
    pass


# ============================================================================
# DELIVERY TRACKING
# ============================================================================

class DeliveryTrackingSchema(BaseSchema):
    """Track order status changes"""
    __tablename__ = "delivery_tracking"

    order_id = db.Column(db.String, db.ForeignKey("customer_orders.uid"), nullable=False, index=True)
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False)
    telecaller_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False)
    delivery_person_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True)
    status_changed_to = db.Column(db.String(50), nullable=False)
    
    # New delivery specific fields
    postpone_date = db.Column(db.Date, nullable=True)
    attempt_number = db.Column(db.Integer, default=1, nullable=False)
    priority_level = db.Column(db.Integer, default=0, nullable=False, index=True)
    
    remarks = db.Column(db.Text)
    changed_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False)

    # Relationships
    order = relationship("CustomerOrderSchema", back_populates="delivery_tracking")
    outlet = relationship("OutletSchema", back_populates="delivery_tracking")
    delivery_person = relationship("UserSchema", foreign_keys=[delivery_person_id])
    changer = relationship("UserSchema", foreign_keys=[changed_by])


class DeliveryTrackingManager(GenericManager[DeliveryTrackingSchema]):
    pass


# ============================================================================
# ACTIVITY LOGS
# ============================================================================

class ActivityLogSchema(BaseSchema):
    """Audit trail for all actions"""
    __tablename__ = "activity_logs"

    user_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False, index=True)
    action = db.Column(db.String(255), nullable=False)
    entity_type = db.Column(db.String(100), nullable=False, index=True)
    entity_id = db.Column(db.String, nullable=True)
    details = db.Column(db.JSON)
    ip_address = db.Column(db.String(45))

    # Relationships
    user = relationship("UserSchema", back_populates="activity_logs")


class ActivityLogManager(GenericManager[ActivityLogSchema]):
    pass


# ============================================================================
# NOTIFICATIONS
# ============================================================================

class NotificationSchema(BaseSchema):
    """User notifications"""
    __tablename__ = "notifications"

    user_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(50), default="info", nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)

    # Relationships
    user = relationship("UserSchema", back_populates="notifications")


class NotificationManager(GenericManager[NotificationSchema]):
    pass


# ============================================================================
# SYSTEM CONFIGURATION
# ============================================================================

class SystemConfigurationSchema(BaseSchema):
    """Global system settings for invoicing and company info"""
    __tablename__ = "system_configuration"

    company_name = db.Column(db.String(255), nullable=False)
    company_address = db.Column(db.Text, nullable=False)
    company_gstin = db.Column(db.String(15), nullable=False)
    company_pan = db.Column(db.String(10), nullable=False)
    company_logo_url = db.Column(db.String(500))
    invoice_terms = db.Column(db.Text)
    invoice_footer = db.Column(db.Text)
    cgst_default_rate = db.Column(db.Numeric(5, 2), default=9.00)
    sgst_default_rate = db.Column(db.Numeric(5, 2), default=9.00)
    igst_default_rate = db.Column(db.Numeric(5, 2), default=18.00)
    price_includes_tax = db.Column(db.Boolean, default=False, nullable=False)


class SystemConfigurationManager(GenericManager[SystemConfigurationSchema]):
    pass


# ============================================================================
# OUTLET DAILY COLLECTIONS
# ============================================================================

class OutletDailyCollectionSchema(BaseSchema):
    """Daily payment collections from outlets"""
    __tablename__ = "outlet_daily_collections"

    date = db.Column(db.Date, nullable=False, index=True)
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, index=True)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    payment_mode = db.Column(db.Enum(OutletPaymentMode), nullable=False)
    payment_sub_mode = db.Column(db.Enum(OutletPaymentSubMode), nullable=False)
    transaction_id = db.Column(db.String(255), nullable=True)
    remarks = db.Column(db.Text, nullable=True)
    
    # Status tracking
    confirmation_status = db.Column(db.Enum(OutletCollectionStatus), nullable=False, default=OutletCollectionStatus.PENDING, index=True)
    confirmed_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True)
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    
    # Relationships
    outlet = relationship("OutletSchema", foreign_keys=[outlet_id])
    confirmer = relationship("UserSchema", foreign_keys=[confirmed_by])


class OutletDailyCollectionManager(GenericManager[OutletDailyCollectionSchema]):
    pass


# ============================================================================
# OUTLET MANAGER PAYOUTS
# ============================================================================

class OutletManagerPayoutSchema(BaseSchema):
    """Monthly payouts to outlet managers"""
    __tablename__ = "outlet_manager_payouts"

    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, index=True)
    outlet_manager_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False, index=True)
    period_from = db.Column(db.Date, nullable=False, index=True)
    period_to = db.Column(db.Date, nullable=False, index=True)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    payment_date = db.Column(db.Date, nullable=True, index=True)
    payment_method = db.Column(db.Enum(PaymentMethod), nullable=False)
    transaction_id = db.Column(db.String(255), nullable=True)
    status = db.Column(db.Enum(PayoutStatus), nullable=False, default=PayoutStatus.PENDING, index=True)
    remarks = db.Column(db.Text, nullable=True)
    
    # Audit fields
    created_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False)
    approved_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True)
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    paid_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True)
    paid_at = db.Column(db.DateTime(timezone=True), nullable=True)
    
    # Relationships
    outlet = relationship("OutletSchema", foreign_keys=[outlet_id])
    outlet_manager = relationship("UserSchema", foreign_keys=[outlet_manager_id])
    creator = relationship("UserSchema", foreign_keys=[created_by])
    approver = relationship("UserSchema", foreign_keys=[approved_by])
    payer = relationship("UserSchema", foreign_keys=[paid_by])


class OutletManagerPayoutManager(GenericManager[OutletManagerPayoutSchema]):
    pass


# ============================================================================
# LSQ Telecallers Mapping
# ============================================================================

class LSQTelecallerMappingSchema(BaseSchema):
    """LSQ Telecaller Mapping"""
    __tablename__ = "lsq_telecaller_mapping"

    lsq_id = db.Column(db.String, nullable=False, index=True)
    telecaller_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False, index=True)
    lsq_email = db.Column(db.String, nullable=False)
    
    # Relationships
    telecaller = relationship("UserSchema", foreign_keys=[telecaller_id])


class LSQTelecallerMappingManager(GenericManager[LSQTelecallerMappingSchema]):
    pass


# ============================================================================
# OUTLET MAPPING (replaces Google Sheets auto-assign logic)
# ============================================================================
class OutletMappingSchema(BaseSchema):
    """Outlet mapping for district/taluk to outlet assignment"""
    __tablename__ = "outlet_mappings"

    state = db.Column(db.String(100), nullable=False, index=True)
    district = db.Column(db.String(100), nullable=False, index=True)
    taluk = db.Column(db.String(100), nullable=True, index=True)
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Relationships
    outlet = relationship("OutletSchema", foreign_keys=[outlet_id])


class OutletMappingManager(GenericManager[OutletMappingSchema]):
    pass

# ============================================================================
# DELIVERY GUYS MANAGEMENT
# ============================================================================

class DeliveryGuySchema(BaseSchema):
    """Delivery guys for order and transfer assignments"""
    __tablename__ = "delivery_guys"

    user_id = db.Column(db.String, db.ForeignKey("users.uid"), unique=True, nullable=False, index=True)
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, index=True)
    is_active_for_delivery = db.Column(db.Boolean, default=True, nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)

    # Relationships
    user = relationship("UserSchema", foreign_keys=[user_id])
    outlet = relationship("OutletSchema", foreign_keys=[outlet_id])

class DeliveryGuyManager(GenericManager[DeliveryGuySchema]):
    pass

# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    # User Management
    "UserSchema", "UserManager",
    
    # Outlet Management
    "OutletSchema", "OutletManager",
    
    # Product Management
    "ProductCategorySchema", "ProductCategoryManager",
    "ProductSchema", "ProductManager",
    
    # Inventory
    "InventorySchema", "InventoryManager",
    
    # Invoices
    "SalesInvoiceSchema", "SalesInvoiceManager",
    "SalesInvoiceItemSchema", "SalesInvoiceItemManager",
    
    # Orders
    "CustomerOrderSchema", "CustomerOrderManager",
    "OrderItemSchema", "OrderItemManager",
    "OrderTransactionSchema", "OrderTransactionManager",
    
    # Sales
    "SalesTransactionSchema", "SalesTransactionManager",
    
    # Stock Transfers
    "StockTransferOrderSchema", "StockTransferOrderManager",
    "TransferItemSchema", "TransferItemManager",
    
    # Tracking
    "DeliveryTrackingSchema", "DeliveryTrackingManager",
    
    # System
    "ActivityLogSchema", "ActivityLogManager",
    "NotificationSchema", "NotificationManager",
    "SystemConfigurationSchema", "SystemConfigurationManager",
    
    # Outlet Collections
    "OutletDailyCollectionSchema", "OutletDailyCollectionManager",
    
    # Outlet Manager Payouts
    "OutletManagerPayoutSchema", "OutletManagerPayoutManager",

    # LSQ Telecaller Mapping
    "LSQTelecallerMappingSchema", "LSQTelecallerMappingManager",

    # Outlet Mapping (auto-assign)
    "OutletMappingSchema", "OutletMappingManager",

    # Delivery Guys
    "DeliveryGuySchema", "DeliveryGuyManager",
]
