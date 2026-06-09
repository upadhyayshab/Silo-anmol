import sqlalchemy as db
from sqlalchemy.orm import relationship, aliased, joinedload, selectinload, load_only, defer
from sqlalchemy.orm.relationships import RelationshipProperty
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy import and_, or_, not_, case, func
from sqlalchemy.sql import text
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, date, timezone, timedelta
from enum import Enum
from typing import Optional, List, Dict, Any, Union
# ============================================================================


from SharedBackend.managers import BaseSchema, GenericManager, BasePassSchema, BasePassManager
from SharedBackend.managers.base import NESTED_JOINS, NESTED_FILTERS
from utils.constants import (
    UserRole, OrderStatus, CollectionType, PaymentMethod,
    PaymentStatus, InvoiceType, TransferStatus, UnitOfMeasure,
    OutletPaymentMode, OutletPaymentSubMode, OutletCollectionStatus,
    PayoutStatus, PayoutFrequency, OutletType, SmartpingJobStatus,
    AuditStatus
)


# ============================================================================
# ERP BASE MANAGERS (EXTENDING SHAREDBACKEND)
# ============================================================================

class ERPGenericManager[SchemaType: BaseSchema](GenericManager[SchemaType]):
    @classmethod
    def _resolve_joins(cls, schema: type[BaseSchema], joins: Any, loader: Any = None) -> list[Any]:
        """
        Smarter join resolver that uses selectinload for collections and joinedload for scalars.
        Supports QueryableAttribute, list, and dict for joins.
        """
        if joins is None:
            return []

        # Convert single join to list for uniform processing
        if not isinstance(joins, (list, dict, set)):
            joins_list = [joins]
        elif isinstance(joins, set):
            joins_list = list(joins)
        elif isinstance(joins, dict):
            joins_list = [joins]
        else:
            joins_list = joins

        options = []
        for join in joins_list:
            if isinstance(join, QueryableAttribute):
                prop = join.property
                if isinstance(prop, RelationshipProperty):
                    # Efficiency: selectinload for collections, joinedload for scalars
                    if prop.uselist:
                        options.append(loader.selectinload(join) if loader else selectinload(join))
                    else:
                        options.append(loader.joinedload(join) if loader else joinedload(join))
            
            elif isinstance(join, dict):
                for key, sub_joins in join.items():
                    attr = getattr(schema, key) if isinstance(key, str) else key
                    prop = attr.property
                    
                    if isinstance(prop, RelationshipProperty):
                        if prop.uselist:
                            current_loader = loader.selectinload(attr) if loader else selectinload(attr)
                        else:
                            current_loader = loader.joinedload(attr) if loader else joinedload(attr)
                        
                        if sub_joins:
                            options.extend(cls._resolve_joins(prop.mapper.class_, sub_joins, current_loader))
                        else:
                            options.append(current_loader)
            
            elif isinstance(join, (list, tuple)) and len(join) > 0:
                # Handle [parent, child1, child2] or [parent, [child, grandchild]]
                parent = join[0]
                children = join[1:]
                
                attr = getattr(schema, parent) if isinstance(parent, str) else parent
                prop = attr.property
                
                if isinstance(prop, RelationshipProperty):
                    if prop.uselist:
                        current_loader = loader.selectinload(attr) if loader else selectinload(attr)
                    else:
                        current_loader = loader.joinedload(attr) if loader else joinedload(attr)
                    
                    if not children:
                        options.append(current_loader)
                    else:
                        for child in children:
                            options.extend(cls._resolve_joins(prop.mapper.class_, child, current_loader))

        return options

    @classmethod
    def _cls_ops(
            cls,
            schema: type[BaseSchema],
            query: db.Select = None,
            joins: list[NESTED_JOINS] = None,
            include: list[str] = None,
            exclude: list[str] = None,
    ) -> db.Select:
        """
        Improved _ops that can be used as a class method and uses the smarter _resolve_joins.
        """
        assert not (include and exclude), "parameters `include` and `exclude` are mutually exclusive"
        
        query = db.select(schema) if query is None else query
        
        if joins:
            resolved_options = cls._resolve_joins(schema, joins)
            if resolved_options:
                query = query.options(*resolved_options)
        
        if include:
            query = query.options(load_only(
                *(getattr(schema, col.name) for col in schema.__table__.columns if col.name in include)
            ))
            
        if exclude:
            query = query.options(defer(*(getattr(schema, col) for col in exclude)))
            
        return query

    def _ops(
            self,
            query: db.Select = None,
            joins: list[NESTED_JOINS] = None,
            include: list[str] = None,
            exclude: list[str] = None,
    ) -> db.Select:
        """Instance-level override to use the new class-level _cls_ops"""
        return self.__class__._cls_ops(self.Schema, query, joins, include, exclude)

    @classmethod
    async def _filter(cls, query: db.Select, filters: NESTED_FILTERS, schema: type[BaseSchema]) -> db.Select:
        """
        Overridden filter method to support dynamic operators ($ilike, $like, $gt, etc.)
        and fix the dictionary fallthrough bug in the base manager.
        """
        operator_mapping = {
            '==': lambda col, val: col == val,
            '$eq': lambda col, val: col == val,
            '!=': lambda col, val: col != val,
            '$neq': lambda col, val: col != val,
            '>': lambda col, val: col > val,
            '$gt': lambda col, val: col > val,
            '>=': lambda col, val: col >= val,
            '$gte': lambda col, val: col >= val,
            '<': lambda col, val: col < val,
            '$lt': lambda col, val: col < val,
            '<=': lambda col, val: col <= val,
            '$lte': lambda col, val: col <= val,
            'in': lambda col, val: col.in_(val if isinstance(val, list) else [val]),
            '$in': lambda col, val: col.in_(val if isinstance(val, list) else [val]),
            '$nin': lambda col, val: ~col.in_(val if isinstance(val, list) else [val]),
            'between': lambda col, val: col.between(val[0], val[1]),
            '$between': lambda col, val: col.between(val[0], val[1]),
            '$like': lambda col, val: col.like(val),
            '$ilike': lambda col, val: col.ilike(val),
            '$ieq': lambda col, val: col.ilike(val),
            '$in_ci': lambda col, val: and_(*[col.ilike(v) for v in val]) if isinstance(val, list) else col.ilike(val)
        }

        # Expand dot notation into nested dictionaries
        # e.g., {"items.product.product_name": "foo"} -> {"items": {"product.product_name": "foo"}}
        # This allows recursive handling of nested relationship filters.
        expanded_filters = {}
        for key, value in filters.items():
            if "." in key:
                parts = key.split(".", 1)
                parent, child = parts[0], parts[1]
                if parent not in expanded_filters:
                    expanded_filters[parent] = {}
                if isinstance(expanded_filters[parent], dict):
                    expanded_filters[parent][child] = value
                else:
                    # Fallback if there's a name collision
                    expanded_filters[key] = value
            else:
                expanded_filters[key] = value
        filters = expanded_filters

        # Handle relationship filters first
        relationship_filters = {}
        insp = db.inspect(schema)
        mapper = insp.mapper if hasattr(insp, "mapper") else insp
        for relation in mapper.relationships:
            if relation.key in filters:
                relationship_filters[relation.key] = filters.pop(relation.key)

        for column, condition in filters.items():
            col_attr = getattr(schema, column, None)
            if col_attr is None:
                continue

            if isinstance(condition, dict):
                # Process each operator in the dictionary
                for op, value in condition.items():
                    if op in operator_mapping:
                        # Handle Date-only comparison with DateTime columns
                        # We use func.date() for equality, inequality, and between to ignore the time part
                        target_col = col_attr
                        is_date = isinstance(value, date) and not isinstance(value, datetime)
                        is_date_list = isinstance(value, list) and len(value) > 0 and all(isinstance(v, date) and not isinstance(v, datetime) for v in value)
                        
                        if isinstance(col_attr.type, (db.DateTime, db.DATETIME)) and (is_date or is_date_list):
                            if op in ['==', '$eq', '!=', '$neq', 'between', '$between', 'in', '$in', '$nin']:
                                target_col = func.date(col_attr)
                        
                        query = query.filter(operator_mapping[op](target_col, value))
            elif isinstance(condition, list):
                # Check if it's a list of dates
                if isinstance(col_attr.type, (db.DateTime, db.DATETIME)) and condition and all(isinstance(v, date) and not isinstance(v, datetime) for v in condition):
                    query = query.filter(func.date(col_attr).in_(condition))
                else:
                    query = query.filter(col_attr.in_(condition))
            else:
                # Simple scalar equality
                if isinstance(col_attr.type, (db.DateTime, db.DATETIME)) and isinstance(condition, date) and not isinstance(condition, datetime):
                    query = query.filter(func.date(col_attr) == condition)
                else:
                    query = query.filter(col_attr == condition)


        # Handle relationship joins and nested filters
        for relation_key, related_filter in relationship_filters.items():
            relation = mapper.relationships[relation_key]
            related_model = relation.mapper.class_

            if isinstance(related_filter, dict):
                # Process nested filters by getting the combined condition from a subquery
                subquery = db.select(related_model)
                subquery = await cls._filter(subquery, related_filter, related_model)
                condition = subquery.whereclause
                
                if condition is not None:
                    if not relation.uselist:
                        # For single relationships (one-to-one, many-to-one)
                        query = query.filter(getattr(schema, relation_key).has(condition))
                    else:
                        # For collections (one-to-many, many-to-many)
                        query = query.filter(getattr(schema, relation_key).any(condition))
            
            elif isinstance(related_filter, list) and relation.uselist:
                # Handle list of filters for collections (OR logic between items)
                conditions = []
                for sub_filter in related_filter:
                    sub_subquery = db.select(related_model)
                    sub_subquery = await cls._filter(sub_subquery, sub_filter, related_model)
                    sub_condition = sub_subquery.whereclause
                    if sub_condition is not None:
                        conditions.append(getattr(schema, relation_key).any(sub_condition))
                if conditions:
                    query = query.filter(and_(*conditions))
            
            elif isinstance(related_filter, list) and not relation.uselist:
                # Fallback for list on single relationship (though rare)
                # Treat as OR of conditions on the same related object
                conditions = []
                for sub_filter in related_filter:
                    sub_subquery = db.select(related_model)
                    sub_subquery = await cls._filter(sub_subquery, sub_filter, related_model)
                    sub_condition = sub_subquery.whereclause
                    if sub_condition is not None:
                        conditions.append(sub_condition)
                if conditions:
                    query = query.filter(getattr(schema, relation_key).has(or_(*conditions)))

        return query

    async def get_aggregated_data(
        self,
        group_by: List[Any],
        aggregations: List[Any],
        filters: Dict[str, Any] = None,
        joins: List[Any] = None,
        session: AsyncSession = None
    ) -> List[Any]:
        """
        Generic aggregation method that supports grouping, explicit joins, and dynamic filters.
        """
        query = db.select(*group_by, *aggregations)
        
        if joins:
            for join_target in joins:
                if isinstance(join_target, tuple) and len(join_target) == 2:
                    query = query.join(join_target[0], join_target[1])
                else:
                    query = query.join(join_target)
        
        if filters:
            query = await self._filter(query, filters, self.Schema)
            
        query = query.group_by(*group_by)
        
        async def execute(s):
            record = await s.execute(query)
            return record.unique().all()

        if session:
            return await execute(session)
        
        async with self.session_factory() as session:
            return await execute(session)


class ERPBasePassManager[SchemaType: BaseSchema](BasePassManager[SchemaType], ERPGenericManager[SchemaType]):
    """Extends BasePassManager with the improved filtering logic from ERPGenericManager"""
    pass


# ============================================================================
# USER MANAGEMENT
# ============================================================================

class UserSchema(BasePassSchema):
    """User accounts with role-based access"""
    __tablename__ = "users"

    email = db.Column(db.String(255), nullable=False, index=True)
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


class UserManager(ERPBasePassManager[UserSchema]):
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
    outlet_type = db.Column(db.Enum(OutletType, values_callable=lambda x: [e.value for e in x]), default=OutletType.OUTLET, nullable=False)

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
    inventory_audits = relationship("InventoryAuditSchema", back_populates="outlet")


class OutletManager(ERPGenericManager[OutletSchema]):
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


class ProductCategoryManager(ERPGenericManager[ProductCategorySchema]):
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
    inventory_audit_items = relationship("InventoryAuditItemSchema", back_populates="product")
    order_items = relationship("OrderItemSchema", back_populates="product")
    invoice_items = relationship("SalesInvoiceItemSchema", back_populates="product")
    sales = relationship("SalesTransactionSchema", back_populates="product")
    transfer_items = relationship("TransferItemSchema", back_populates="product")


class ProductManager(ERPGenericManager[ProductSchema]):
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


class InventoryManager(ERPGenericManager[InventorySchema]):
    pass


# ============================================================================
# INVENTORY AUDIT MANAGEMENT
# ============================================================================

class InventoryAuditSchema(BaseSchema):
    """Weekly inventory audits submitted by outlets"""
    __tablename__ = "inventory_audits"

    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, index=True)
    audit_date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    status = db.Column(db.Enum(AuditStatus), default=AuditStatus.PENDING, nullable=False, index=True)
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    submitted_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True)
    match_percentage = db.Column(db.Numeric(5, 2), nullable=True)

    # Relationships
    outlet = relationship("OutletSchema", back_populates="inventory_audits")
    submitter = relationship("UserSchema", foreign_keys=[submitted_by])
    items = relationship("InventoryAuditItemSchema", back_populates="audit", cascade="all, delete-orphan")


class InventoryAuditManager(ERPGenericManager[InventoryAuditSchema]):
    pass


class InventoryAuditItemSchema(BaseSchema):
    """Line items for inventory audits"""
    __tablename__ = "inventory_audit_items"

    audit_id = db.Column(db.String, db.ForeignKey("inventory_audits.uid"), nullable=False, index=True)
    product_id = db.Column(db.String, db.ForeignKey("products.uid"), nullable=False, index=True)
    system_quantity = db.Column(db.Integer, default=0, nullable=True)
    physical_quantity = db.Column(db.Integer, nullable=True)

    # Relationships
    audit = relationship("InventoryAuditSchema", back_populates="items")
    product = relationship("ProductSchema", back_populates="inventory_audit_items")


class InventoryAuditItemManager(ERPGenericManager[InventoryAuditItemSchema]):
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


class SalesInvoiceManager(ERPGenericManager[SalesInvoiceSchema]):
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


class SalesInvoiceItemManager(ERPGenericManager[SalesInvoiceItemSchema]):
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

    # Rider Payout fields
    rider_earning = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)
    payout_id = db.Column(db.String, db.ForeignKey("rider_payouts.uid"), nullable=True, index=True)

    # Relationships
    telecaller = relationship("UserSchema", back_populates="created_orders", foreign_keys=[telecaller_id]) # here we will get the telecallers and the outlet manager
    assigned_outlet = relationship("OutletSchema", back_populates="orders")
    delivery_person = relationship("UserSchema", foreign_keys=[delivery_person_id])
    items = relationship("OrderItemSchema", back_populates="order", cascade="all, delete-orphan")
    transactions = relationship("OrderTransactionSchema", back_populates="order", cascade="all, delete-orphan")
    delivery_tracking = relationship("DeliveryTrackingSchema", back_populates="order", cascade="all, delete-orphan")
    lsq_order_ad = relationship("LSQOrderAdSchema", back_populates="order",cascade="all, delete-orphan")


class CustomerOrderManager(ERPGenericManager[CustomerOrderSchema]):
    async def get_orders_count(self, filters: Dict[str, Any] = None, session: AsyncSession = None) -> int:
        """Get the count of orders based on dynamic filters"""
        if filters is None:
            filters = {}
        
        # Use the overridden _filter from ERPGenericManager to support dynamic operators
        query = db.select(db.func.count(CustomerOrderSchema.uid))
        query = await self._filter(query, filters, CustomerOrderSchema)
        
        if session:
            result = await session.execute(query)
            return result.scalar() or 0
        
        async with self.session_factory() as session:
            result = await session.execute(query)
            return result.scalar() or 0

    async def get_orders_count_grouped(self, group_by: Union[str, List[str]], filters: Dict[str, Any] = None, session: AsyncSession = None) -> List[Dict[str, Any]]:
        """Get the count of orders grouped by one or more columns based on dynamic filters"""
        if filters is None:
            filters = {}
        
        is_single = isinstance(group_by, str)
        cols = [group_by] if is_single else group_by
            
        group_attrs = []
        for col in cols:
            col_attr = getattr(CustomerOrderSchema, col, None)
            if col_attr is None:
                raise ValueError(f"Invalid group_by column: {col}")
            group_attrs.append(col_attr)

        query = db.select(*group_attrs, db.func.count(CustomerOrderSchema.uid))
        query = await self._filter(query, filters, CustomerOrderSchema)
        query = query.group_by(*group_attrs)
        
        async def execute(s):
            res = await s.execute(query)
            all_rows = res.all()
            results = []
            for row in all_rows:
                if is_single:
                    results.append({"key": row[0], "count": row[1]})
                else:
                    item = {cols[i]: row[i] for i in range(len(cols))}
                    item["count"] = row[-1]
                    results.append(item)
            return results

        if session:
            return await execute(session)
        
        async with self.session_factory() as session:
            return await execute(session)

    async def get_daily_order_summary(
        self, 
        start_date: date, 
        end_date: date, 
        outlet_id: Optional[str] = None,
        session: AsyncSession = None
    ) -> List[Dict[str, Any]]:
        """ Get daily order summary including volume, revenue, and quantity by status. """
                
        # Subquery for total quantity per order
        item_subq = (
            db.select(
                OrderItemSchema.order_id,
                func.sum(OrderItemSchema.quantity).label("total_qty")
            )
            .group_by(OrderItemSchema.order_id)
            .subquery()
        )
        
        # Timezone conversion for order_date (Asia/Kolkata)
        # Assuming co.order_date is stored in UTC
        date_col = func.date(CustomerOrderSchema.order_date.op('AT TIME ZONE')('Asia/Kolkata'))
        
        # Revenue calculation: gross_amount - discount_applied
        revenue_expr = CustomerOrderSchema.gross_amount - CustomerOrderSchema.discount_applied
        qty_expr = func.coalesce(item_subq.c.total_qty, 0)
        
        # Helper for status-based filtering in aggregations
        def status_case(status, expr):
            return func.sum(case((CustomerOrderSchema.order_status == status, expr), else_=0))

        def status_count(status):
            return func.count(case((CustomerOrderSchema.order_status == status, CustomerOrderSchema.uid), else_=None))

        query = (
            db.select(
                date_col.label("date"),
                # Total Placed
                func.count(CustomerOrderSchema.uid).label("total_placed_orders"),
                func.sum(revenue_expr).label("total_placed_revenue"),
                func.sum(qty_expr).label("total_placed_quantity"),
                # Delivered
                status_count(OrderStatus.DELIVERED).label("delivered_orders"),
                status_case(OrderStatus.DELIVERED, revenue_expr).label("delivered_revenue"),
                status_case(OrderStatus.DELIVERED, qty_expr).label("delivered_quantity"),
                # Cancelled
                status_count(OrderStatus.CANCELLED).label("cancelled_orders"),
                status_case(OrderStatus.CANCELLED, revenue_expr).label("cancelled_revenue"),
                status_case(OrderStatus.CANCELLED, qty_expr).label("cancelled_quantity"),
                # Pending
                status_count(OrderStatus.PENDING).label("pending_orders"),
                status_case(OrderStatus.PENDING, revenue_expr).label("pending_revenue"),
                status_case(OrderStatus.PENDING, qty_expr).label("pending_quantity")
            )
            .outerjoin(item_subq, CustomerOrderSchema.uid == item_subq.c.order_id)
            .where(date_col.between(start_date, end_date))
        )
        
        if outlet_id:
            query = query.where(CustomerOrderSchema.assigned_outlet_id == outlet_id)
            
        query = query.group_by(date_col).order_by(text("date ASC"))
        
        async def execute(s):
            res = await s.execute(query)
            rows = res.all()
            results = []
            for row in rows:
                results.append({
                    "date": row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                    "total_placed": {
                        "orders": int(row.total_placed_orders),
                        "revenue": float(row.total_placed_revenue or 0),
                        "quantity": int(row.total_placed_quantity or 0)
                    },
                    "delivered": {
                        "orders": int(row.delivered_orders),
                        "revenue": float(row.delivered_revenue or 0),
                        "quantity": int(row.delivered_quantity or 0)
                    },
                    "cancelled": {
                        "orders": int(row.cancelled_orders),
                        "revenue": float(row.cancelled_revenue or 0),
                        "quantity": int(row.cancelled_quantity or 0)
                    },
                    "pending": {
                        "orders": int(row.pending_orders),
                        "revenue": float(row.pending_revenue or 0),
                        "quantity": int(row.pending_quantity or 0)
                    }
                })
            return results

        if session:
            return await execute(session)
        
        async with self.session_factory() as session:
            return await execute(session)


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


class OrderItemManager(ERPGenericManager[OrderItemSchema]):
    async def get_quantity_by_status(self, filters: Dict[str, Any] = None, session: AsyncSession = None) -> List[Dict[str, Any]]:
        """
        Get product quantity counts grouped by order status.
        Uses the generic aggregation feature with explicit joins.
        """
        group_by = [ProductSchema,CustomerOrderSchema.order_status]
        aggregations = [db.func.sum(OrderItemSchema.quantity).label("total_quantity")]
        joins = [
        (CustomerOrderSchema, OrderItemSchema.order_id == CustomerOrderSchema.uid),
        (ProductSchema, OrderItemSchema.product_id == ProductSchema.uid)
        ]
        
        rows = await self.get_aggregated_data(
            group_by=group_by,
            aggregations=aggregations,
            filters=filters,
            joins=joins,
            session=session
        )
        results = []
        for row in rows:
            product_obj = row[0]
            status_enum = row[1]
            quantity = row[2]
            results.append({
                "product": {
                    "uid": product_obj.uid,
                    "product_name": product_obj.product_name
                },
                "order_status": status_enum.value,
                "quantity": quantity
            })
        return results

    async def get_outlet_product_summary(self, filters: Dict[str, Any] = None, session: AsyncSession = None) -> List[Dict[str, Any]]:
        """
        Get product summary grouped by outlet, status, product name and variant (SKU).
        """
        group_by = [
            OutletSchema.uid,
            OutletSchema.outlet_name,
            CustomerOrderSchema.order_status,
            ProductSchema.uid,
            ProductSchema.product_name,
            ProductSchema.sku
        ]
        aggregations = [
            db.func.sum(OrderItemSchema.quantity).label("total_quantity"),
            db.func.sum(OrderItemSchema.subtotal).label("total_amount")
        ]
        joins = [
            (CustomerOrderSchema, OrderItemSchema.order_id == CustomerOrderSchema.uid),
            (ProductSchema, OrderItemSchema.product_id == ProductSchema.uid),
            (OutletSchema, CustomerOrderSchema.assigned_outlet_id == OutletSchema.uid)
        ]
        
        rows = await self.get_aggregated_data(
            group_by=group_by,
            aggregations=aggregations,
            filters=filters,
            joins=joins,
            session=session
        )
        
        results = []
        for row in rows:
            results.append({
                "outlet_id": row[0],
                "outlet": row[1],
                "status": row[2].value if hasattr(row[2], 'value') else row[2],
                "product_id": row[3],
                "product": row[4],
                "variant": row[5],
                "quantity": int(row[6]) if row[6] is not None else 0,
                "amount": float(row[7]) if row[7] is not None else 0.0
            })

        return results

    async def get_product_quantity_report(
        self,
        start_date: date,
        end_date: date,
        session: AsyncSession = None
    ) -> List[Dict[str, Any]]:
        """Get product quantity grouped by date, outlet, status, and product for reporting."""
        date_col = func.date(
            CustomerOrderSchema.order_date.op('AT TIME ZONE')('Asia/Kolkata')
        )
        outlet_name = func.coalesce(OutletSchema.outlet_name, 'Unassigned')

        query = (
            db.select(
                date_col.label("date"),
                outlet_name.label("outlet"),
                CustomerOrderSchema.order_status,
                ProductSchema.product_name,
                ProductSchema.sku,
                func.sum(OrderItemSchema.quantity).label("total_quantity"),
                func.count(CustomerOrderSchema.uid.distinct()).label("order_count"),
                func.string_agg(CustomerOrderSchema.order_number.distinct(), ', ').label("order_numbers")
            )
            .join(CustomerOrderSchema, OrderItemSchema.order_id == CustomerOrderSchema.uid)
            .join(ProductSchema, OrderItemSchema.product_id == ProductSchema.uid)
            .outerjoin(OutletSchema, CustomerOrderSchema.assigned_outlet_id == OutletSchema.uid)
            .where(date_col.between(start_date, end_date))
            .group_by(
                date_col, outlet_name,
                CustomerOrderSchema.order_status,
                ProductSchema.product_name, ProductSchema.sku
            )
            .order_by(text("date DESC"), func.sum(OrderItemSchema.quantity).desc())
        )

        async def execute(s):
            res = await s.execute(query)
            rows = res.all()
            return [
                {
                    "date": row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                    "outlet": row.outlet,
                    "status": row.order_status.value if hasattr(row.order_status, 'value') else row.order_status,
                    "product": row.product_name,
                    "sku": row.sku,
                    "total_quantity": int(row.total_quantity or 0),
                    "order_count": int(row.order_count or 0),
                    "order_numbers": row.order_numbers or ""
                }
                for row in rows
            ]

        if session:
            return await execute(session)
        async with self.session_factory() as session:
            return await execute(session)


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


class OrderTransactionManager(ERPGenericManager[OrderTransactionSchema]):
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


class SalesTransactionManager(ERPGenericManager[SalesTransactionSchema]):
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


class StockTransferOrderManager(ERPGenericManager[StockTransferOrderSchema]):
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


class TransferItemManager(ERPGenericManager[TransferItemSchema]):
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


class DeliveryTrackingManager(ERPGenericManager[DeliveryTrackingSchema]):
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


class ActivityLogManager(ERPGenericManager[ActivityLogSchema]):
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


class NotificationManager(ERPGenericManager[NotificationSchema]):
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


class SystemConfigurationManager(ERPGenericManager[SystemConfigurationSchema]):
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


class OutletDailyCollectionManager(ERPGenericManager[OutletDailyCollectionSchema]):
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


class OutletManagerPayoutManager(ERPGenericManager[OutletManagerPayoutSchema]):
    pass


# ============================================================================
# LSQ Telecallers Mapping
# ============================================================================

class LSQTelecallerMappingSchema(BaseSchema):
    """LSQ Telecaller Mapping"""
    __tablename__ = "lsq_telecaller_mapping"

    lsq_id = db.Column(db.String, nullable=False, unique=True, index=True)
    telecaller_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False, index=True)
    lsq_email = db.Column(db.String, nullable=False)
    
    # Relationships
    telecaller = relationship("UserSchema", foreign_keys=[telecaller_id])


class LSQTelecallerMappingManager(ERPGenericManager[LSQTelecallerMappingSchema]):
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
    pincode = db.Column(db.String(10), nullable=True, index=True)
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Relationships
    outlet = relationship("OutletSchema", foreign_keys=[outlet_id])


class OutletMappingManager(ERPGenericManager[OutletMappingSchema]):
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
    payout_frequency = db.Column(db.Enum(PayoutFrequency), default=PayoutFrequency.WEEKLY, nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)

    # Relationships
    user = relationship("UserSchema", foreign_keys=[user_id])
    outlet = relationship("OutletSchema", foreign_keys=[outlet_id])

class DeliveryGuyManager(ERPGenericManager[DeliveryGuySchema]):
    pass

# ============================================================================
# DELIVERY GUY HANDOVERS
# ============================================================================

class DeliveryGuyHandoverSchema(BaseSchema):
    """Tracking cash handovers from delivery guys to outlet"""
    __tablename__ = "delivery_guy_handovers"

    delivery_guy_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False, index=True)
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, index=True)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    handover_date = db.Column(db.Date, nullable=False, index=True)
    
    # Status tracking
    status = db.Column(db.Enum(OutletCollectionStatus), nullable=False, default=OutletCollectionStatus.PENDING, index=True)
    confirmed_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True)
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    remarks = db.Column(db.Text, nullable=True)
    
    # Relationships
    delivery_guy = relationship("UserSchema", foreign_keys=[delivery_guy_id])
    outlet = relationship("OutletSchema", foreign_keys=[outlet_id])
    confirmer = relationship("UserSchema", foreign_keys=[confirmed_by])


class DeliveryGuyHandoverManager(ERPGenericManager[DeliveryGuyHandoverSchema]):
    pass


# ============================================================================
# RIDER RATE CARDS
# ============================================================================

class RateCardSchema(BaseSchema):
    """Rider rate cards for delivery incentives"""
    __tablename__ = "rate_cards"

    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, unique=True, index=True)
    pay_per_order = db.Column(db.Numeric(10, 2), default=0.00, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Relationships
    outlet = relationship("OutletSchema", foreign_keys=[outlet_id])


class RateCardManager(ERPGenericManager[RateCardSchema]):
    pass


# ============================================================================
# RIDER PAYOUTS
# ============================================================================

class RiderPayoutSchema(BaseSchema):
    """Historical records of rider payouts"""
    __tablename__ = "rider_payouts"

    rider_id = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False, index=True)
    outlet_id = db.Column(db.String, db.ForeignKey("outlets.uid"), nullable=False, index=True)
    period_from = db.Column(db.Date, nullable=False)
    period_to = db.Column(db.Date, nullable=False)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.Enum(PayoutStatus), default=PayoutStatus.PENDING, nullable=False, index=True)
    payment_method = db.Column(db.Enum(PaymentMethod), nullable=True)
    transaction_id = db.Column(db.String(255), nullable=True)
    payment_date = db.Column(db.Date, nullable=True)
    remarks = db.Column(db.Text, nullable=True)

    # Audit fields
    created_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=False)
    paid_by = db.Column(db.String, db.ForeignKey("users.uid"), nullable=True)

    # Relationships
    rider = relationship("UserSchema", foreign_keys=[rider_id])
    outlet = relationship("OutletSchema", foreign_keys=[outlet_id])
    creator = relationship("UserSchema", foreign_keys=[created_by])
    payer = relationship("UserSchema", foreign_keys=[paid_by])
    orders = relationship("CustomerOrderSchema", backref="payout", foreign_keys=[CustomerOrderSchema.payout_id])


class RiderPayoutManager(ERPGenericManager[RiderPayoutSchema]):
    pass

# ============================================================================
# LSQ ORDER AD
# ============================================================================

class LSQOrderAdSchema(BaseSchema):
    __tablename__ = "lsq_order_ad"

    lead_id = db.Column(db.String(255), nullable=False)
    order_id = db.Column(db.String, db.ForeignKey("customer_orders.uid"), nullable=False, index=True)
    lead_source = db.Column(db.String(255), nullable=True)
    source_campaign = db.Column(db.String(255), nullable=True)
    ad_id = db.Column(db.String(255), nullable=True)
    ad_set_id = db.Column(db.String(255), nullable=True)
    campaign_id = db.Column(db.String(255), nullable=True)
    lead_stage = db.Column(db.String(255), nullable=True)
    utm_param = db.Column(db.JSON, nullable=True)

    order = relationship("CustomerOrderSchema", foreign_keys=[order_id])

class LSQOrderAdManager(ERPGenericManager[LSQOrderAdSchema]):
    pass

# ============================================================================
# SMARTPING DRIP CAMPAIGNS
# ============================================================================


class SmartpingCampaignRegistrySchema(BaseSchema):
    __tablename__ = "smartping_campaign_registry"

    event_key = db.Column(db.String(255), nullable=False, unique=True, index=True)
    business_event_key = db.Column(db.String(255), nullable=False, index=True)
    campaign_name = db.Column(db.String(255), nullable=False)
    provider = db.Column(db.String(64), nullable=False, default="smartping", index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    version = db.Column(db.Integer, default=1, nullable=False)
    trigger_delay_value = db.Column(db.Integer, default=0, nullable=False)
    trigger_delay_unit = db.Column(db.String(16), default="minutes", nullable=False)
    template_param_keys = db.Column(db.JSON, nullable=False, default=list)
    media = db.Column(db.JSON, nullable=True)
    buttons = db.Column(db.JSON, nullable=True)
    attributes = db.Column(db.JSON, nullable=True)
    tags = db.Column(db.JSON, nullable=True)
    params_fallback_value = db.Column(db.JSON, nullable=True)
    source = db.Column(db.String(255), nullable=True)
    notes = db.Column(db.Text, nullable=True)


class SmartpingCampaignRegistryManager(ERPGenericManager[SmartpingCampaignRegistrySchema]):
    async def fetch_active_by_event_key(
        self,
        event_key: str,
        *,
        session: AsyncSession = None,
    ) -> SmartpingCampaignRegistrySchema:
        return await self.fetch_one(
            filters={"event_key": event_key, "is_active": True},
            sorts=["-version"],
            session=session,
        )

    async def fetch_active_by_business_event_key(
        self,
        business_event_key: str,
        *,
        session: AsyncSession = None,
    ) -> List[SmartpingCampaignRegistrySchema]:
        records = await self.fetch_all(
            filters={"business_event_key": business_event_key, "is_active": True},
            sorts=["trigger_delay_value", "event_key"],
            session=session,
        )
        return records.items


class SmartpingMessageJobSchema(BaseSchema):
    __tablename__ = "smartping_message_jobs"

    event_key = db.Column(db.String(255), nullable=False, index=True)
    business_event_key = db.Column(db.String(255), nullable=False, index=True)
    business_event_ref = db.Column(db.String(255), nullable=False, index=True)
    registry_uid = db.Column(db.String, db.ForeignKey("smartping_campaign_registry.uid"), nullable=False, index=True)
    destination = db.Column(db.String(32), nullable=False, index=True)
    user_name = db.Column(db.String(255), nullable=False)
    send_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    status = db.Column(db.Enum(SmartpingJobStatus, name="smartping_job_status"), default=SmartpingJobStatus.PENDING, nullable=False, index=True)
    retry_count = db.Column(db.Integer, default=0, nullable=False)
    max_retries = db.Column(db.Integer, default=3, nullable=False)
    idempotency_key = db.Column(db.String(255), nullable=False, unique=True, index=True)
    context_payload = db.Column(db.JSON, nullable=True)
    request_payload = db.Column(db.JSON, nullable=True)
    response_payload = db.Column(db.JSON, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    locked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    failed_at = db.Column(db.DateTime(timezone=True), nullable=True)


class SmartpingMessageJobManager(ERPGenericManager[SmartpingMessageJobSchema]):
    async def claim_due_jobs(
        self,
        limit: int = 100,
        *,
        session: AsyncSession = None,
    ) -> List[SmartpingMessageJobSchema]:
        now = datetime.now(timezone.utc)
        if session is None:
            async with self.session_factory() as session:
                jobs = await self.claim_due_jobs(limit=limit, session=session)
                await session.commit()
                return jobs

        query = (
            db.select(self.Schema)
            .where(
                self.Schema.status == SmartpingJobStatus.PENDING,
                self.Schema.send_at <= now,
            )
            .order_by(self.Schema.send_at.asc(), self.Schema.uid.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        records = await session.execute(query)
        jobs = list(records.unique().scalars())
        for job in jobs:
            job.status = SmartpingJobStatus.LOCKED
            job.locked_at = now
        if jobs:
            await session.flush()
        return jobs

    async def get_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        session: AsyncSession = None,
    ) -> Optional[SmartpingMessageJobSchema]:
        try:
            return await self.fetch_one(
                filters={"idempotency_key": idempotency_key},
                session=session,
            )
        except Exception:
            return None


def smartping_delay_to_timedelta(value: int, unit: str):
    unit = (unit or "minutes").lower()
    if unit == "seconds":
        return timedelta(seconds=value)
    if unit == "minutes":
        return timedelta(minutes=value)
    if unit == "hours":
        return timedelta(hours=value)
    if unit == "days":
        return timedelta(days=value)
    return timedelta(minutes=value)
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

    # LSQ Telecallers Mapping
    "LSQTelecallerMappingSchema", "LSQTelecallerMappingManager",

    # Outlet Mapping (auto-assign)
    "OutletMappingSchema", "OutletMappingManager",

    # Delivery Guys
    "DeliveryGuySchema", "DeliveryGuyManager",

    # LSQ Order Ad
    "LSQOrderAdSchema", "LSQOrderAdManager",

    # SmartPing Drip Campaigns
    "SmartpingCampaignRegistrySchema", "SmartpingCampaignRegistryManager",
    "SmartpingMessageJobSchema", "SmartpingMessageJobManager",
    "smartping_delay_to_timedelta",
    
    # Delivery Guy Handovers
    "DeliveryGuyHandoverSchema", "DeliveryGuyHandoverManager",

    # Rider Payouts & Rate Cards
    "RateCardSchema", "RateCardManager",
    "RiderPayoutSchema", "RiderPayoutManager",
]

