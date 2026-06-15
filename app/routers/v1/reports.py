from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional, Dict, Any
from datetime import datetime, date, timedelta
from decimal import Decimal
from sqlalchemy import text

from config import get_settings, get_engine
from managers import (
    SalesInvoiceManager, CustomerOrderManager, InventoryManager,
    ProductManager, OutletManager, UserManager, StockTransferOrderManager,
    ActivityLogManager,CustomerOrderSchema, OrderItemManager, OrderItemSchema,
    DeliveryGuyManager, DeliveryGuyHandoverManager, OrderTransactionManager,
    DeliveryGuySchema, DeliveryGuyHandoverSchema, OrderTransactionSchema
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, OrderStatus, TransferStatus, PaymentStatus, PaymentMethod, OutletCollectionStatus
from utils.functions import ensure_date
import calendar
from utils import dependencies as D

settings = get_settings()
engine = get_engine(settings.name)

# Initialize managers
invoice_manager = SalesInvoiceManager(engine)
order_manager = CustomerOrderManager(engine)
inventory_manager = InventoryManager(engine)
product_manager = ProductManager(engine)
outlet_manager = OutletManager(engine)
user_manager = UserManager(engine)
transfer_manager = StockTransferOrderManager(engine)
activity_manager = ActivityLogManager(engine)
delivery_guy_manager = DeliveryGuyManager(engine)
handover_manager = DeliveryGuyHandoverManager(engine)
transaction_manager = OrderTransactionManager(engine)
order_item_manager = OrderItemManager(engine)

router = APIRouter(prefix="/reports", tags=["Reports & Analytics"])

@router.get("/order-products-quantity-count")
async def order_products_quantity_count(
    filters: Dict[str, Any] = Depends(D.filtering_dependency),
    # _: str = Depends(require_roles(
    #     UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT, UserRole.OUTLET_MANAGER
    # ))
):
    try:
        # Fetch the flat aggregation data from manager
        flat_results = await order_item_manager.get_quantity_by_status(filters=filters)
        
        return flat_results
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch products quantity count: {str(e)}"
        )



@router.get("/dashboard-overview")
async def get_outlet_orders(
    outlet_id: str,
    role: Optional[str] = None,
    order_status: Optional[OrderStatus] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    limit: int = 100,
    offset: int = 0,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT, UserRole.OUTLET_MANAGER
    ))
):
    """
    Get all orders for a specific outlet (assigned OR walk-in)
    Returns order_id, total_amount, and status for each order
    role : should be either "TELECALLER" or "OUTLET_MANAGER" for the filter 
    """
    try:
        # Get current user for access control
        current_user = await user_manager.fetch(current_user_id)
        
        # Access control: outlet managers can only view their own outlet
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id != outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view orders for your outlet"
                )
        
        # Verify outlet exists
        try:
            outlet = await outlet_manager.fetch(outlet_id)
        except:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Outlet not found"
            )
        
        # Build filters
        filters = {"assigned_outlet_id": outlet_id}
        if order_status:
            filters["order_status"] = order_status
            
        # Fetch all orders for this outlet
        orders_result = await order_manager.fetch_all(
            filters=filters,
            limit=limit,
            offset=offset,
            joins=[CustomerOrderSchema.telecaller, (CustomerOrderSchema.items, OrderItemSchema.product)]
        )
        
        # Process orders and apply date filtering
        order_list = []
        status_counts = {s.value: 0 for s in OrderStatus}
        commission_summary = {
            "total_commission": 0.0,
            "by_status": {s.value: 0.0 for s in OrderStatus}
        }
        
        for order in orders_result.items:
            # Apply date filtering if specified
            order_date = ensure_date(order.order_date)
            if from_date and order_date < from_date:
                continue
            if to_date and order_date > to_date:
                continue
            
            # 2. Extract Creator Info
            creator_role_value = None
            if order.telecaller:
                order.creater_name = order.telecaller.full_name
                # Assuming order.telecaller.role is an Enum or String (e.g., "TELECALLER")
                creator_role_value = getattr(order.telecaller.role, 'value', order.telecaller.role)
                order.is_telecaller_order = (creator_role_value == "TELECALLER")
            else:
                order.creater_name = "System/Unknown"
                order.is_telecaller_order = False

            # 3. ROLE FILTER LOGIC
            # If role is provided, only keep records where creator_role matches
            # If role is None/Empty, this block is skipped (shows all)
            if role and creator_role_value != role:
                continue
                
            # Count status
            status_counts[order.order_status.value] += 1
            
            # Calculate commission (with backward compatibility)
            order_commission = float(getattr(order, 'total_commission', 0.0))
            commission_summary["total_commission"] += order_commission
            commission_summary["by_status"][order.order_status.value] += order_commission
            
            # Build items list
            order_items = []
            for item in (order.items or []):
                product = getattr(item, 'product', None)
                order_items.append({
                    "product_id": item.product_id,
                    "product_name": product.product_name if product else item.product_id,
                    "quantity": item.quantity,
                    "unit_price": float(item.unit_price),
                    "total_price": float(item.total_price) if getattr(item, 'total_price', None) is not None else float(item.quantity * item.unit_price),
                })

            # Build order data
            order_data = {
                "order_id": order.uid,
                "order_number": order.order_number,
                "total_amount": float(order.total_amount),  # Final price after all calculations
                "status": order.order_status.value,  # OrderStatus enum value
                "customer_name": order.customer_name,
                "customer_phone": order.customer_phone,
                "order_date": order.order_date.isoformat(),
                "actual_delivery_date": order.actual_delivery_date.isoformat() if order.actual_delivery_date else None,
                "collection_type": order.collection_type.value,
                "gross_amount": float(order.gross_amount),
                "manual_discount": float(order.manual_discount),
                "discount_applied": float(order.discount_applied),
                "prepaid_amount": float(order.prepaid_amount),
                "is_telecaller_order": order.is_telecaller_order,
                "creater_name": order.creater_name,
                "telecaller_id": order.telecaller_id if order.telecaller_id else "",
                "telecaller_phone": order.telecaller.phone if order.telecaller and order.telecaller.phone else "",
                "telecaller_role": creator_role_value if creator_role_value else "",
                "items": order_items,
                "total_commission": order_commission  # NEW: Commission data per order
            }
            
            order_list.append(order_data)
        
        # Sort by order_date (newest first)
        order_list.sort(key=lambda x: x["order_date"], reverse=True)
        
        return {
            "outlet_id": outlet_id,
            "outlet_name": outlet.outlet_name,
            "total_orders": len(order_list),
            "orders": order_list,
            "status_summary": status_counts,
            "commission_summary": commission_summary,  # NEW: Commission summary by status
            "filters_applied": {
                "role": role if role else "All",
                "status": order_status.value if order_status else None,
                "from_date": from_date.isoformat() if from_date else None,
                "to_date": to_date.isoformat() if to_date else None,
                "limit": limit,
                "offset": offset
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch outlet orders: {str(e)}"
        )


@router.get("/dashboard/outlet/{outlet_id}")
async def get_outlet_dashboard(
    outlet_id: str,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Get outlet-specific dashboard metrics
    """
    try:
        # Check permissions
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id != outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view your outlet's dashboard"
                )
        
        today = date.today()
        month_start = today.replace(day=1)
        
        # Get outlet data
        outlet = await outlet_manager.fetch(outlet_id)
        
        # Sales data
        invoices = await invoice_manager.fetch_all(
            filters={"outlet_id": outlet_id, "is_cancelled": False}
        )
        
        current_month_sales = sum(
            float(inv.total_amount) for inv in invoices.items
            if inv.invoice_date >= month_start
        )
        
        today_sales = sum(
            float(inv.total_amount) for inv in invoices.items
            if inv.invoice_date == today
        )
        
        # Order data
        orders = await order_manager.fetch_all(
            filters={"assigned_outlet_id": outlet_id}
        )
        
        pending_orders = len([
            order for order in orders.items
            if order.order_status == OrderStatus.PENDING
        ])
        
        # Inventory data
        inventory = await inventory_manager.fetch_all(
            filters={"outlet_id": outlet_id}
        )
        
        low_stock_items = []
        for inv_item in inventory.items:
            product = await product_manager.fetch(inv_item.product_id)
            available = inv_item.quantity - inv_item.reserved_quantity
            if available < product.min_stock_level:
                low_stock_items.append({
                    "product_name": product.product_name,
                    "available": available,
                    "min_level": product.min_stock_level
                })
        
        return {
            "outlet_name": outlet.outlet_name,
            "current_month_sales": current_month_sales,
            "today_sales": today_sales,
            "pending_orders": pending_orders,
            "total_inventory_items": len(inventory.items),
            "low_stock_count": len(low_stock_items),
            "low_stock_items": low_stock_items[:5],  # Top 5 low stock items
            "last_updated": datetime.utcnow()
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch outlet dashboard: {str(e)}"
        )


@router.get("/sales/summary")
async def get_sales_summary(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    outlet_id: Optional[str] = None,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Get comprehensive sales summary report
    """
    try:
        # Set default date range
        if not from_date:
            from_date = date.today().replace(day=1)
        if not to_date:
            to_date = date.today()
        
        # Role-based filtering
        current_user = await user_manager.fetch(current_user_id)
        filters = {"is_cancelled": False}
        
        if current_user.role == UserRole.OUTLET_MANAGER:
            filters["outlet_id"] = current_user.outlet_id
        elif outlet_id and current_user.role in [UserRole.ACCOUNTANT, UserRole.ADMIN, UserRole.SUPER_ADMIN]:
            filters["outlet_id"] = outlet_id
        
        invoices = await invoice_manager.fetch_all(filters=filters)
        
        # Filter by date range
        period_invoices = [
            inv for inv in invoices.items
            if from_date <= inv.invoice_date <= to_date
        ]
        
        # Calculate metrics
        total_sales = sum(float(inv.total_amount) for inv in period_invoices)
        total_tax = sum(float(inv.total_tax) for inv in period_invoices)
        total_invoices = len(period_invoices)
        
        # Daily breakdown
        daily_sales = {}
        for inv in period_invoices:
            date_str = inv.invoice_date.strftime("%Y-%m-%d")
            if date_str not in daily_sales:
                daily_sales[date_str] = {"date": date_str, "sales": 0, "invoices": 0}
            daily_sales[date_str]["sales"] += float(inv.total_amount)
            daily_sales[date_str]["invoices"] += 1
        
        # Payment method breakdown
        payment_methods = {}
        for inv in period_invoices:
            method = inv.payment_method.value
            if method not in payment_methods:
                payment_methods[method] = {"count": 0, "amount": 0}
            payment_methods[method]["count"] += 1
            payment_methods[method]["amount"] += float(inv.total_amount)
        
        return {
            "period": {"from_date": from_date, "to_date": to_date},
            "summary": {
                "total_sales": total_sales,
                "total_tax": total_tax,
                "total_invoices": total_invoices,
                "average_invoice_value": total_sales / total_invoices if total_invoices > 0 else 0
            },
            "daily_sales": list(daily_sales.values()),
            "payment_methods": payment_methods
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate sales summary: {str(e)}"
        )


@router.get("/inventory/analysis")
async def get_inventory_analysis(
    outlet_id: Optional[str] = None,
    current_user_id: str = Depends(require_roles(
        UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Get comprehensive inventory analysis
    """
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {}
        if current_user.role == UserRole.OUTLET_MANAGER:
            filters["outlet_id"] = current_user.outlet_id
        elif current_user.role == UserRole.WAREHOUSE_MANAGER:
            filters["outlet_id"] = None  # Warehouse only
        elif outlet_id is not None:
            filters["outlet_id"] = outlet_id
        
        inventory_items = await inventory_manager.fetch_all(filters=filters)
        
        total_items = len(inventory_items.items)
        total_stock_value = 0
        low_stock_items = []
        out_of_stock_items = []
        high_stock_items = []
        
        for inv_item in inventory_items.items:
            product = await product_manager.fetch(inv_item.product_id)
            available = inv_item.quantity - inv_item.reserved_quantity
            stock_value = float(product.cost_price) * inv_item.quantity
            total_stock_value += stock_value
            
            if available <= 0:
                out_of_stock_items.append({
                    "product_name": product.product_name,
                    "sku": product.sku,
                    "available": available,
                    "reserved": inv_item.reserved_quantity
                })
            elif available < product.min_stock_level:
                low_stock_items.append({
                    "product_name": product.product_name,
                    "sku": product.sku,
                    "available": available,
                    "min_level": product.min_stock_level,
                    "shortage": product.min_stock_level - available
                })
            elif available > product.min_stock_level * 3:  # High stock threshold
                high_stock_items.append({
                    "product_name": product.product_name,
                    "sku": product.sku,
                    "available": available,
                    "min_level": product.min_stock_level,
                    "excess": available - product.min_stock_level
                })
        
        return {
            "summary": {
                "total_items": total_items,
                "total_stock_value": total_stock_value,
                "out_of_stock_count": len(out_of_stock_items),
                "low_stock_count": len(low_stock_items),
                "high_stock_count": len(high_stock_items)
            },
            "out_of_stock_items": out_of_stock_items[:10],
            "low_stock_items": sorted(low_stock_items, key=lambda x: x["shortage"], reverse=True)[:10],
            "high_stock_items": sorted(high_stock_items, key=lambda x: x["excess"], reverse=True)[:10]
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate inventory analysis: {str(e)}"
        )


@router.get("/orders/performance")
async def get_order_performance(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    current_user_id: str = Depends(require_roles(
        UserRole.TELECALLER, UserRole.OUTLET_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Get order performance metrics
    """
    try:
        if not from_date:
            from_date = date.today().replace(day=1)
        if not to_date:
            to_date = date.today()
        
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {}
        if current_user.role == UserRole.TELECALLER:
            filters["telecaller_id"] = current_user_id
        elif current_user.role == UserRole.OUTLET_MANAGER:
            filters["assigned_outlet_id"] = current_user.outlet_id
        
        orders = await order_manager.fetch_all(filters=filters)
        
        # Filter by date range
        period_orders = [
            order for order in orders.items
            if from_date <= ensure_date(order.order_date) <= to_date
        ]
        
        # Calculate metrics
        total_orders = len(period_orders)
        total_value = sum(float(order.total_amount) for order in period_orders)
        
        status_breakdown = {}
        for order in period_orders:
            os_val = order.order_status.value
            if os_val not in status_breakdown:
                status_breakdown[os_val] = {"count": 0, "value": 0}
            status_breakdown[os_val]["count"] += 1
            status_breakdown[os_val]["value"] += float(order.total_amount)
        
        # Delivery performance
        delivered_orders = [
            order for order in period_orders
            if order.order_status == OrderStatus.DELIVERED and order.actual_delivery_date
        ]
        
        avg_delivery_time = 0
        if delivered_orders:
            total_delivery_time = sum(
                (ensure_date(order.actual_delivery_date) - ensure_date(order.order_date)).days
                for order in delivered_orders
            )
            avg_delivery_time = total_delivery_time / len(delivered_orders)
        
        return {
            "period": {"from_date": from_date, "to_date": to_date},
            "summary": {
                "total_orders": total_orders,
                "total_value": total_value,
                "average_order_value": total_value / total_orders if total_orders > 0 else 0,
                "delivered_orders": len(delivered_orders),
                "delivery_rate": len(delivered_orders) / total_orders * 100 if total_orders > 0 else 0,
                "avg_delivery_time_days": round(avg_delivery_time, 1)
            },
            "status_breakdown": status_breakdown
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate order performance report: {str(e)}"
        )


@router.get("/financial/summary")
async def get_financial_summary(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    outlet_id: Optional[str] = None,
    current_user_id: str = Depends(require_roles(
        UserRole.ACCOUNTANT, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Get financial summary including P&L basics
    """
    try:
        if not from_date:
            from_date = date.today().replace(day=1)
        if not to_date:
            to_date = date.today()
        
        filters = {"is_cancelled": False}
        if outlet_id:
            filters["outlet_id"] = outlet_id
        
        invoices = await invoice_manager.fetch_all(filters=filters)
        
        # Filter by date range
        period_invoices = [
            inv for inv in invoices.items
            if from_date <= inv.invoice_date <= to_date
        ]
        
        # Revenue calculations
        gross_revenue = sum(float(inv.total_amount) for inv in period_invoices)
        total_tax_collected = sum(float(inv.total_tax) for inv in period_invoices)
        net_revenue = gross_revenue - total_tax_collected
        
        # Cost calculations (simplified - using cost price from products)
        total_cost = 0
        for inv in period_invoices:
            # Get invoice items to calculate cost
            from managers import SalesInvoiceItemManager
            item_manager = SalesInvoiceItemManager(engine)
            items = await item_manager.fetch_all(filters={"invoice_id": inv.uid})
            
            for item in items.items:
                product = await product_manager.fetch(item.product_id)
                total_cost += float(product.cost_price) * item.quantity
        
        gross_profit = net_revenue - total_cost
        gross_margin = (gross_profit / net_revenue * 100) if net_revenue > 0 else 0
        
        # Tax breakdown
        total_cgst = sum(float(inv.cgst_amount) for inv in period_invoices)
        total_sgst = sum(float(inv.sgst_amount) for inv in period_invoices)
        total_igst = sum(float(inv.igst_amount) for inv in period_invoices)
        
        return {
            "period": {"from_date": from_date, "to_date": to_date},
            "revenue": {
                "gross_revenue": gross_revenue,
                "net_revenue": net_revenue,
                "total_invoices": len(period_invoices)
            },
            "costs": {
                "cost_of_goods_sold": total_cost,
                "gross_profit": gross_profit,
                "gross_margin_percent": round(gross_margin, 2)
            },
            "taxes": {
                "total_tax_collected": total_tax_collected,
                "cgst_collected": total_cgst,
                "sgst_collected": total_sgst,
                "igst_collected": total_igst
            }
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate financial summary: {str(e)}"
        )


@router.get("/products/performance")
async def get_product_performance(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    outlet_id: Optional[str] = None,
    limit: int = 20,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Get product performance analytics based on DELIVERED orders only
    
    This endpoint counts only products that have been delivered to customers,
    using actual_delivery_date for date filtering.
    """
    try:
        if not from_date:
            from_date = date.today().replace(day=1)
        if not to_date:
            to_date = date.today()
        
        current_user = await user_manager.fetch(current_user_id)
        
        # Build filters for DELIVERED orders only
        filters = {"order_status": OrderStatus.DELIVERED}
        if current_user.role == UserRole.OUTLET_MANAGER:
            filters["assigned_outlet_id"] = current_user.outlet_id
        elif outlet_id:
            filters["assigned_outlet_id"] = outlet_id
        
        # Fetch all delivered orders
        orders = await order_manager.fetch_all(filters=filters, limit=0)
        
        # Filter by actual_delivery_date (when product was actually delivered)
        period_orders = []
        for order in orders.items:
            if not order.actual_delivery_date:
                continue
            
            # Handle both date and datetime objects
            delivery_date = ensure_date(order.actual_delivery_date)
            
            if from_date <= delivery_date <= to_date:
                period_orders.append(order)
        
        # Get order items for delivered orders
        from managers import OrderItemManager
        order_item_manager = OrderItemManager(engine)
        
        product_performance = {}
        
        # Cache for product details to avoid duplicate fetches
        product_cache = {}
        
        for order in period_orders:
            # Fetch ALL items for this order (limit=0 means no limit)
            items = await order_item_manager.fetch_all(
                filters={"order_id": order.uid},
                limit=0  # Fetch all items, not just default limit
            )
            
            for item in items.items:
                # Fetch product details only once per product
                if item.product_id not in product_cache:
                    try:
                        product_cache[item.product_id] = await product_manager.fetch(item.product_id)
                    except:
                        # Skip if product not found
                        continue
                
                product = product_cache[item.product_id]
                
                # Initialize product performance tracking
                if item.product_id not in product_performance:
                    product_performance[item.product_id] = {
                        "product_name": product.product_name,
                        "sku": product.sku,
                        "quantity_sold": 0,
                        "revenue": 0,
                        "cost": 0,
                        "profit": 0,
                        "transactions": 0
                    }
                
                # Update performance metrics
                perf = product_performance[item.product_id]
                perf["quantity_sold"] += item.quantity
                perf["revenue"] += float(item.subtotal)  # Use subtotal from order item
                perf["transactions"] += 1
                
                # Calculate cost and profit
                item_cost = float(product.cost_price) * item.quantity
                perf["cost"] += item_cost
                perf["profit"] += float(item.subtotal) - item_cost
        
        # Sort by revenue and get top performers
        top_products = sorted(
            product_performance.values(),
            key=lambda x: x["revenue"],
            reverse=True
        )[:limit]
        
        # Calculate profit margins
        for product in top_products:
            if product["revenue"] > 0:
                product["profit_margin"] = round(product["profit"] / product["revenue"] * 100, 2)
            else:
                product["profit_margin"] = 0
        
        return {
            "period": {"from_date": from_date, "to_date": to_date},
            "top_products": top_products,
            "summary": {
                "total_products_sold": len(product_performance),
                "total_quantity": sum(p["quantity_sold"] for p in product_performance.values()),
                "total_revenue": sum(p["revenue"] for p in product_performance.values())
            }
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate product performance report: {str(e)}"
        )


@router.get("/activity-logs")
async def get_activity_logs(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    user_id: Optional[str] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    current_user_id: str = Depends(require_roles(
        UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Get system activity logs for audit purposes
    """
    try:
        if not from_date:
            from_date = date.today() - timedelta(days=7)  # Last 7 days
        if not to_date:
            to_date = date.today()
        
        filters = {}
        if user_id:
            filters["user_id"] = user_id
        if action:
            filters["action"] = action
        if entity_type:
            filters["entity_type"] = entity_type
        
        logs = await activity_manager.fetch_all(
            filters=filters,
            limit=limit,
            offset=offset
        )
        
        # Filter by date range
        period_logs = [
            log for log in logs.items
            if from_date <= ensure_date(log.created_at) <= to_date
        ]
        
        # Get user details for logs
        enriched_logs = []
        for log in period_logs:
            try:
                user = await user_manager.fetch(log.user_id)
                enriched_logs.append({
                    "uid": log.uid,
                    "user_name": user.full_name,
                    "user_email": user.email,
                    "action": log.action,
                    "entity_type": log.entity_type,
                    "entity_id": log.entity_id,
                    "details": log.details,
                    "ip_address": log.ip_address,
                    "timestamp": log.created_at
                })
            except:
                # Handle case where user might be deleted
                enriched_logs.append({
                    "uid": log.uid,
                    "user_name": "Unknown User",
                    "user_email": "unknown@example.com",
                    "action": log.action,
                    "entity_type": log.entity_type,
                    "entity_id": log.entity_id,
                    "details": log.details,
                    "ip_address": log.ip_address,
                    "timestamp": log.created_at
                })
        
        # Activity summary
        action_summary = {}
        user_summary = {}
        
        for log in enriched_logs:
            # Action summary
            action = log["action"]
            if action not in action_summary:
                action_summary[action] = 0
            action_summary[action] += 1
            
            # User summary
            user = log["user_name"]
            if user not in user_summary:
                user_summary[user] = 0
            user_summary[user] += 1
        
        return {
            "period": {"from_date": from_date, "to_date": to_date},
            "logs": enriched_logs,
            "summary": {
                "total_activities": len(enriched_logs),
                "unique_users": len(user_summary),
                "action_breakdown": action_summary,
                "top_active_users": sorted(
                    user_summary.items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:5]
            }
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch activity logs: {str(e)}"
        )


@router.get("/transfers/efficiency")
async def get_transfer_efficiency(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    current_user_id: str = Depends(require_roles(
        UserRole.WAREHOUSE_MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """
    Get stock transfer efficiency metrics
    """
    try:
        if not from_date:
            from_date = date.today().replace(day=1)
        if not to_date:
            to_date = date.today()
        
        transfers = await transfer_manager.fetch_all()
        
        # Filter by date range
        period_transfers = [
            transfer for transfer in transfers.items
            if from_date <= ensure_date(transfer.created_at) <= to_date
        ]
        
        total_transfers = len(period_transfers)
        
        # Status breakdown
        status_counts = {}
        for transfer in period_transfers:
            ts_val = transfer.status.value
            if ts_val not in status_counts:
                status_counts[ts_val] = 0
            status_counts[ts_val] += 1
        
        # Delivery time analysis
        delivered_transfers = [
            t for t in period_transfers
            if t.status == TransferStatus.DELIVERED and t.delivered_date
        ]
        
        avg_delivery_time = 0
        if delivered_transfers:
            total_time = sum(
                (ensure_date(t.delivered_date) - ensure_date(t.created_at)).days
                for t in delivered_transfers
            )
            avg_delivery_time = total_time / len(delivered_transfers)
        
        # Outlet performance
        outlet_performance = {}
        for transfer in period_transfers:
            outlet_id = transfer.to_outlet_id
            if outlet_id not in outlet_performance:
                try:
                    outlet = await outlet_manager.fetch(outlet_id)
                    outlet_performance[outlet_id] = {
                        "outlet_name": outlet.outlet_name,
                        "total_requests": 0,
                        "completed": 0,
                        "pending": 0,
                        "cancelled": 0
                    }
                except:
                    continue
            
            perf = outlet_performance[outlet_id]
            perf["total_requests"] += 1
            
            if transfer.status == TransferStatus.DELIVERED:
                perf["completed"] += 1
            elif transfer.status in [TransferStatus.PENDING, TransferStatus.APPROVED, TransferStatus.IN_TRANSIT]:
                perf["pending"] += 1
            elif transfer.status == TransferStatus.CANCELLED:
                perf["cancelled"] += 1
        
        # Calculate completion rates
        for outlet_id, perf in outlet_performance.items():
            if perf["total_requests"] > 0:
                perf["completion_rate"] = round(perf["completed"] / perf["total_requests"] * 100, 1)
            else:
                perf["completion_rate"] = 0
        
        return {
            "period": {"from_date": from_date, "to_date": to_date},
            "summary": {
                "total_transfers": total_transfers,
                "completed_transfers": len(delivered_transfers),
                "completion_rate": round(len(delivered_transfers) / total_transfers * 100, 1) if total_transfers > 0 else 0,
                "avg_delivery_time_days": round(avg_delivery_time, 1)
            },
            "status_breakdown": status_counts,
            "outlet_performance": list(outlet_performance.values())
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate transfer efficiency report: {str(e)}"
        )


@router.get("/delivery-overview")
async def get_delivery_overview(
    current_user_id: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    Get comprehensive delivery operations overview for Super Admin
    """
    try:
        # 1. Fetch all delivery guys
        delivery_guys = await delivery_guy_manager.fetch_all(limit=1000, filters={"is_deleted": False})
        total_riders = len(delivery_guys.items)
        active_riders = len([dg for dg in delivery_guys.items if dg.is_active_for_delivery])
        
        # 2. Total Deliveries
        delivered_orders = await order_manager.fetch_all(
            filters={"order_status": OrderStatus.DELIVERED},
            limit=50000
        )
        total_deliveries = len(delivered_orders.items)
        
        # 3. Global Pending Cash Calculation
        # Note: We sum cash balances from all riders
        # Total Collected (Cash Transactions received by riders)
        rider_user_ids = {dg.user_id for dg in delivery_guys.items}
        transactions = await transaction_manager.fetch_all(
            filters={
                "payment_method": PaymentMethod.CASH,
                "payment_status": PaymentStatus.PAID
            },
            limit=50000
        )
        total_collected = sum(((t.amount_paid or Decimal("0.00")) for t in transactions.items if t.received_by in rider_user_ids), Decimal("0.00"))
        
        # Total Handed Over (Confirmed handovers)
        handovers = await handover_manager.fetch_all(
            filters={"status": OutletCollectionStatus.CONFIRMED},
            limit=10000
        )
        total_handed_over = sum(((h.amount or Decimal("0.00")) for h in handovers.items), Decimal("0.00"))
        
        total_pending_cash = total_collected - total_handed_over
        
        # 4. Pre-group data by outlet for efficiency (O(N) instead of O(N*M))
        from collections import defaultdict
        
        orders_by_outlet = defaultdict(list)
        for o in delivered_orders.items:
            orders_by_outlet[o.assigned_outlet_id].append(o)
            
        transactions_by_rider = defaultdict(list)
        for t in transactions.items:
            transactions_by_rider[t.received_by].append(t)
            
        handovers_by_outlet = defaultdict(list)
        for h in handovers.items:
            handovers_by_outlet[h.outlet_id].append(h)
            
        # 5. Outlet Performance
        outlets = await outlet_manager.fetch_all(limit=100, filters={"is_active": True})
        outlet_performance_list = []
        
        for outlet in outlets.items:
            outlet_riders = [dg for dg in delivery_guys.items if dg.outlet_id == outlet.uid]
            outlet_rider_user_ids = {dg.user_id for dg in outlet_riders}
            
            outlet_active = len([dg for dg in outlet_riders if dg.is_active_for_delivery])
            
            # Use pre-grouped data
            outlet_delivered_orders = orders_by_outlet.get(outlet.uid, [])
            outlet_deliveries = len(outlet_delivered_orders)
            
            # Pending cash for this outlet
            outlet_collected = Decimal("0.00")
            for rider_id in outlet_rider_user_ids:
                rider_txs = transactions_by_rider.get(rider_id, [])
                outlet_collected += sum(((t.amount_paid or Decimal("0.00")) for t in rider_txs), Decimal("0.00"))
                
            outlet_handovers = handovers_by_outlet.get(outlet.uid, [])
            outlet_handed_over = sum(((h.amount or Decimal("0.00")) for h in outlet_handovers), Decimal("0.00"))
            outlet_pending = outlet_collected - outlet_handed_over
            
            # Calculate On-Time Delivery Rate
            on_time_count = 0
            for o in outlet_delivered_orders:
                actual_date = ensure_date(o.actual_delivery_date)
                expected_date = ensure_date(o.expected_delivery_date)
                
                if actual_date and expected_date and actual_date <= expected_date:
                    on_time_count += 1
            
            on_time_rate = 0
            if outlet_deliveries > 0:
                on_time_rate = round((on_time_count / outlet_deliveries) * 100, 1)
            
            outlet_performance_list.append({
                "id": outlet.uid,
                "name": outlet.outlet_name,
                "code": outlet.outlet_code,
                "riders": len(outlet_riders),
                "active": outlet_active,
                "deliveries": outlet_deliveries,
                "pendingCash": float(outlet_pending),
                "onTimeRate": f"{on_time_rate}%"
            })
            
        return {
            "total_riders": total_riders,
            "active_riders": active_riders,
            "total_deliveries": total_deliveries,
            "total_pending_cash": float(total_pending_cash),
            "outlet_performance": outlet_performance_list
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error in delivery-overview: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch delivery overview: {str(e)}"
        )

# --- Common SQL Snippets for Order Summaries ---
FILTER_CLASSIFICATION_SQL = """
               -- Step 1: Assign the Main Filter Category
               CASE 
                   WHEN co.uid LIKE 'order_%' THEN 'D2C'
                   WHEN (co.uid LIKE 'customer_orders_%' OR co.uid LIKE 'customer_user_%') AND u.role = 'OUTLET_MANAGER' THEN 'Organic'
                   WHEN LOWER(lsq.lead_source) IN (
                        'referral sites', 'direct traffic', 'social media', 'inbound email', 
                        'inbound phone call', 'outbound phone call', 'pay per click ads', 
                        'fb lead ads', 'web visit/login', 'whatsapp inbound', 'app sign up', 
                        'gau swasth subscriber', 'add to cart', 'browsed 3 pages'
                   ) THEN 'Lead Gen'
                   ELSE 'Organic'
               END AS main_filter,

               -- Step 2: Assign the Sub-Filter Category
               CASE 
                   -- D2C Sub-Filter Logic (Checking JSON sources for meta, google, or direct)
                   WHEN co.uid LIKE 'order_%' THEN 
                       CASE 
                           -- Meta matching: check utm_source
                           WHEN LOWER(lsq.utm_param->'utm_source_1'->>'utm_source') LIKE '%meta%' 
                             OR LOWER(lsq.utm_param->'utm_source_2'->>'utm_source') LIKE '%meta%' THEN 'meta'
                           
                           -- Google matching: check utm_source
                           WHEN LOWER(lsq.utm_param->'utm_source_1'->>'utm_source') LIKE '%google%' 
                             OR LOWER(lsq.utm_param->'utm_source_2'->>'utm_source') LIKE '%google%' THEN 'google'
                           
                           -- Direct matching: check utm_source, or fallback to direct if null/empty
                           WHEN LOWER(lsq.utm_param->'utm_source_1'->>'utm_source') LIKE '%direct%' 
                             OR LOWER(lsq.utm_param->'utm_source_2'->>'utm_source') LIKE '%direct%'
                             OR lsq.utm_param IS NULL 
                             OR lsq.utm_param::text = '{}' THEN 'direct'    
                           ELSE 'other_d2c'
                       END
                   
                   -- Organic Sub-Filter Logic
                   WHEN (co.uid LIKE 'customer_orders_%' OR co.uid LIKE 'customer_user_%') AND u.role = 'OUTLET_MANAGER' THEN 'Outlet Manager'
                   
                   -- Lead Gen Sub-Filter Logic
                   ELSE 
                       CASE 
                           WHEN LOWER(lsq.lead_source) = 'organic search' THEN 'Organic Search'
                           WHEN LOWER(lsq.lead_source) = 'referral sites' THEN 'Referral Sites'
                           WHEN LOWER(lsq.lead_source) = 'direct traffic' THEN 'Direct Traffic'
                           WHEN LOWER(lsq.lead_source) = 'social media' THEN 'Social Media'
                           WHEN LOWER(lsq.lead_source) = 'inbound email' THEN 'Inbound Email'
                           WHEN LOWER(lsq.lead_source) = 'inbound phone call' THEN 'Inbound Phone call'
                           WHEN LOWER(lsq.lead_source) = 'outbound phone call' THEN 'Outbound Phone call'
                           WHEN LOWER(lsq.lead_source) = 'pay per click ads' THEN 'Pay per Click Ads'
                           WHEN LOWER(lsq.lead_source) = 'fb lead ads' THEN 'FB Lead Ads'
                           WHEN LOWER(lsq.lead_source) = 'web visit/login' THEN 'web visit/Login'
                           WHEN LOWER(lsq.lead_source) = 'whatsapp inbound' THEN 'WhatsApp Inbound'
                           WHEN LOWER(lsq.lead_source) = 'app sign up' THEN 'App sign up'
                           WHEN LOWER(lsq.lead_source) = 'gau swasth subscriber' THEN 'Gau swasth Subscriber'
                           WHEN LOWER(lsq.lead_source) = 'add to cart' THEN 'add to cart'
                           WHEN LOWER(lsq.lead_source) = 'browsed 3 pages' THEN 'browsed 3 pages'
                           ELSE COALESCE(lsq.lead_source, 'Unclassified / Others')
                       END
               END AS sub_filter
"""

CLASSIFICATION_QUERY_SQL = f"""
    WITH classified_orders AS (
        SELECT 
            co.uid AS order_id,
            (co.created_at AT TIME ZONE 'Asia/Kolkata')::date AS order_date,
            co.gross_amount - co.discount_applied AS net_amount,
{FILTER_CLASSIFICATION_SQL}
        FROM customer_orders co
        LEFT JOIN lsq_order_ad lsq ON co.uid = lsq.order_id
        LEFT JOIN users u ON co.telecaller_id = u.uid
        WHERE (CAST(:outlet_id AS VARCHAR) IS NULL OR co.assigned_outlet_id = CAST(:outlet_id AS VARCHAR))
    )
    SELECT 
        order_date AS date,
        main_filter,
        sub_filter,
        COUNT(order_id) AS total_orders,
        COALESCE(SUM(net_amount), 0) AS total_revenue
    FROM classified_orders
    WHERE order_date BETWEEN :start_date AND :end_date
      AND (CAST(:main_filter AS VARCHAR) IS NULL OR main_filter = CAST(:main_filter AS VARCHAR))
      AND (CAST(:sub_filter AS VARCHAR) IS NULL OR sub_filter = CAST(:sub_filter AS VARCHAR))
    GROUP BY order_date, main_filter, sub_filter
    ORDER BY order_date, main_filter, sub_filter;
"""

async def _execute_order_summary_queries(conn, from_date: date, to_date: date, target_outlet_id: Optional[str], main_filter: Optional[str], sub_filter: Optional[str], main_query_text: str) -> List[Dict[str, Any]]:
    result = await conn.execute(
        text(main_query_text),
        {
            "start_date": from_date,
            "end_date": to_date,
            "outlet_id": target_outlet_id,
            "main_filter": main_filter,
            "sub_filter": sub_filter
        }
    )
    rows = result.all()

    class_result = await conn.execute(
        text(CLASSIFICATION_QUERY_SQL),
        {
            "start_date": from_date,
            "end_date": to_date,
            "outlet_id": target_outlet_id,
            "main_filter": main_filter,
            "sub_filter": sub_filter
        }
    )
    class_rows = class_result.all()

    classifications_by_date = {}
    for class_row in class_rows:
        d_str = str(class_row.date)
        if d_str not in classifications_by_date:
            classifications_by_date[d_str] = []
        classifications_by_date[d_str].append({
            "main_filter": class_row.main_filter,
            "sub_filter": class_row.sub_filter,
            "orders": int(class_row.total_orders),
            "revenue": float(class_row.total_revenue)
        })

    summary = [
        {
            "date": str(row.date),
            "total_placed": {
                "orders": int(row.total_placed_orders),
                "revenue": float(row.total_placed_revenue),
                "quantity": int(row.total_placed_quantity),
            },
            "delivered": {
                "orders": int(row.delivered_orders),
                "revenue": float(row.delivered_revenue),
                "quantity": int(row.delivered_quantity),
            },
            "cancelled": {
                "orders": int(row.cancelled_orders),
                "revenue": float(row.cancelled_revenue),
                "quantity": int(row.cancelled_quantity),
            },
            "pending": {
                "orders": int(row.pending_orders),
                "revenue": float(row.pending_revenue),
                "quantity": int(row.pending_quantity),
            },
            "classifications": classifications_by_date.get(str(row.date), []),
        }
        for row in rows
    ]
    return summary

async def fetch_logistics_order_summary(
    conn,
    from_date: date,
    to_date: date,
    target_outlet_id: Optional[str],
    main_filter: Optional[str] = None,
    sub_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch logistics daily order summary where Placed is based on created_at,
    Pending/Delivered/Cancelled is based on updated_at."""
    DAILY_REV_QUERY = f"""
    WITH dates AS (
        SELECT generate_series(CAST(:start_date AS DATE), CAST(:end_date AS DATE), '1 day'::interval)::date AS d
    ),
    order_stats AS (
        SELECT co.uid, co.created_at, co.updated_at, co.order_status,
               co.gross_amount - co.discount_applied AS net_amount, ot.total_qty,
{FILTER_CLASSIFICATION_SQL}
        FROM customer_orders co
        LEFT JOIN (SELECT order_id, SUM(quantity) AS total_qty FROM order_items GROUP BY order_id) ot
            ON co.uid = ot.order_id
        LEFT JOIN lsq_order_ad lsq ON co.uid = lsq.order_id
        LEFT JOIN users u ON co.telecaller_id = u.uid
        WHERE (CAST(:outlet_id AS VARCHAR) IS NULL OR co.assigned_outlet_id = CAST(:outlet_id AS VARCHAR))
    ),
    filtered_stats AS (
        SELECT * FROM order_stats
        WHERE (CAST(:main_filter AS VARCHAR) IS NULL OR main_filter = CAST(:main_filter AS VARCHAR))
          AND (CAST(:sub_filter AS VARCHAR) IS NULL OR sub_filter = CAST(:sub_filter AS VARCHAR))
    ),
    placed AS (
        SELECT DATE(created_at AT TIME ZONE 'Asia/Kolkata') AS d,
               COUNT(uid) AS total_placed_orders,
               COALESCE(SUM(net_amount), 0) AS total_placed_revenue,
               COALESCE(SUM(total_qty), 0) AS total_placed_quantity
        FROM filtered_stats
        WHERE (created_at AT TIME ZONE 'Asia/Kolkata')::date BETWEEN :start_date AND :end_date
        GROUP BY 1
    ),
    pending AS (
        SELECT DATE(updated_at AT TIME ZONE 'Asia/Kolkata') AS d,
               COUNT(uid) AS pending_orders,
               COALESCE(SUM(net_amount), 0) AS pending_revenue,
               COALESCE(SUM(total_qty), 0) AS pending_quantity
        FROM filtered_stats
        WHERE order_status NOT IN ('DELIVERED', 'CANCELLED')
          AND (updated_at AT TIME ZONE 'Asia/Kolkata')::date BETWEEN :start_date AND :end_date
        GROUP BY 1
    ),
    delivered AS (
        SELECT DATE(updated_at AT TIME ZONE 'Asia/Kolkata') AS d,
               COUNT(uid) AS delivered_orders,
               COALESCE(SUM(net_amount), 0) AS delivered_revenue,
               COALESCE(SUM(total_qty), 0) AS delivered_quantity
        FROM filtered_stats
        WHERE order_status = 'DELIVERED'
          AND (updated_at AT TIME ZONE 'Asia/Kolkata')::date BETWEEN :start_date AND :end_date
        GROUP BY 1
    ),
    cancelled AS (
        SELECT DATE(updated_at AT TIME ZONE 'Asia/Kolkata') AS d,
               COUNT(uid) AS cancelled_orders,
               COALESCE(SUM(net_amount), 0) AS cancelled_revenue,
               COALESCE(SUM(total_qty), 0) AS cancelled_quantity
        FROM filtered_stats
        WHERE order_status = 'CANCELLED'
          AND (updated_at AT TIME ZONE 'Asia/Kolkata')::date BETWEEN :start_date AND :end_date
        GROUP BY 1
    )
    SELECT dates.d AS date,
           COALESCE(p.total_placed_orders, 0) AS total_placed_orders,
           COALESCE(p.total_placed_revenue, 0) AS total_placed_revenue,
           COALESCE(p.total_placed_quantity, 0) AS total_placed_quantity,
           COALESCE(d_stats.delivered_orders, 0) AS delivered_orders,
           COALESCE(d_stats.delivered_revenue, 0) AS delivered_revenue,
           COALESCE(d_stats.delivered_quantity, 0) AS delivered_quantity,
           COALESCE(c_stats.cancelled_orders, 0) AS cancelled_orders,
           COALESCE(c_stats.cancelled_revenue, 0) AS cancelled_revenue,
           COALESCE(c_stats.cancelled_quantity, 0) AS cancelled_quantity,
           COALESCE(pend_stats.pending_orders, 0) AS pending_orders,
           COALESCE(pend_stats.pending_revenue, 0) AS pending_revenue,
           COALESCE(pend_stats.pending_quantity, 0) AS pending_quantity
    FROM dates
    LEFT JOIN placed p ON dates.d = p.d
    LEFT JOIN pending pend_stats ON dates.d = pend_stats.d
    LEFT JOIN delivered d_stats ON dates.d = d_stats.d
    LEFT JOIN cancelled c_stats ON dates.d = c_stats.d
    ORDER BY date ASC;
    """
    
    return await _execute_order_summary_queries(conn, from_date, to_date, target_outlet_id, main_filter, sub_filter, DAILY_REV_QUERY)

async def fetch_marketing_order_summary(
    conn,
    from_date: date,
    to_date: date,
    target_outlet_id: Optional[str],
    main_filter: Optional[str],
    sub_filter: Optional[str]
) -> List[Dict[str, Any]]:
    """Fetch marketing daily order summary where Placed/Pending/Delivered/Cancelled
    are all cohort-grouped by created_at. Includes classifications."""
    MARKETING_REV_QUERY = f"""
    WITH dates AS (
        SELECT generate_series(CAST(:start_date AS DATE), CAST(:end_date AS DATE), '1 day'::interval)::date AS d
    ),
    order_stats AS (
        SELECT co.uid, co.created_at, co.order_status,
               co.gross_amount - co.discount_applied AS net_amount, ot.total_qty,
{FILTER_CLASSIFICATION_SQL}
        FROM customer_orders co
        LEFT JOIN (SELECT order_id, SUM(quantity) AS total_qty FROM order_items GROUP BY order_id) ot
            ON co.uid = ot.order_id
        LEFT JOIN lsq_order_ad lsq ON co.uid = lsq.order_id
        LEFT JOIN users u ON co.telecaller_id = u.uid
        WHERE (CAST(:outlet_id AS VARCHAR) IS NULL OR co.assigned_outlet_id = CAST(:outlet_id AS VARCHAR))
    ),
    cohort_stats AS (
        SELECT DATE(created_at AT TIME ZONE 'Asia/Kolkata') AS d,
               COUNT(uid) AS total_placed_orders,
               COALESCE(SUM(net_amount), 0) AS total_placed_revenue,
               COALESCE(SUM(total_qty), 0) AS total_placed_quantity,
               
               COUNT(uid) FILTER (WHERE order_status = 'DELIVERED') AS delivered_orders,
               COALESCE(SUM(net_amount) FILTER (WHERE order_status = 'DELIVERED'), 0) AS delivered_revenue,
               COALESCE(SUM(total_qty) FILTER (WHERE order_status = 'DELIVERED'), 0) AS delivered_quantity,
               
               COUNT(uid) FILTER (WHERE order_status = 'CANCELLED') AS cancelled_orders,
               COALESCE(SUM(net_amount) FILTER (WHERE order_status = 'CANCELLED'), 0) AS cancelled_revenue,
               COALESCE(SUM(total_qty) FILTER (WHERE order_status = 'CANCELLED'), 0) AS cancelled_quantity,
               
               COUNT(uid) FILTER (WHERE order_status NOT IN ('DELIVERED', 'CANCELLED')) AS pending_orders,
               COALESCE(SUM(net_amount) FILTER (WHERE order_status NOT IN ('DELIVERED', 'CANCELLED')), 0) AS pending_revenue,
               COALESCE(SUM(total_qty) FILTER (WHERE order_status NOT IN ('DELIVERED', 'CANCELLED')), 0) AS pending_quantity
        FROM order_stats
        WHERE (created_at AT TIME ZONE 'Asia/Kolkata')::date BETWEEN :start_date AND :end_date
          AND (CAST(:main_filter AS VARCHAR) IS NULL OR main_filter = CAST(:main_filter AS VARCHAR))
          AND (CAST(:sub_filter AS VARCHAR) IS NULL OR sub_filter = CAST(:sub_filter AS VARCHAR))
        GROUP BY 1
    )
    SELECT dates.d AS date,
           COALESCE(cs.total_placed_orders, 0) AS total_placed_orders,
           COALESCE(cs.total_placed_revenue, 0) AS total_placed_revenue,
           COALESCE(cs.total_placed_quantity, 0) AS total_placed_quantity,
           COALESCE(cs.delivered_orders, 0) AS delivered_orders,
           COALESCE(cs.delivered_revenue, 0) AS delivered_revenue,
           COALESCE(cs.delivered_quantity, 0) AS delivered_quantity,
           COALESCE(cs.cancelled_orders, 0) AS cancelled_orders,
           COALESCE(cs.cancelled_revenue, 0) AS cancelled_revenue,
           COALESCE(cs.cancelled_quantity, 0) AS cancelled_quantity,
           COALESCE(cs.pending_orders, 0) AS pending_orders,
           COALESCE(cs.pending_revenue, 0) AS pending_revenue,
           COALESCE(cs.pending_quantity, 0) AS pending_quantity
    FROM dates
    LEFT JOIN cohort_stats cs ON dates.d = cs.d
    ORDER BY date ASC;
    """
    
    return await _execute_order_summary_queries(conn, from_date, to_date, target_outlet_id, main_filter, sub_filter, MARKETING_REV_QUERY)

@router.get("/daily-order-summary")
async def get_daily_order_summary(
    from_date: date,
    to_date: date,
    outlet_id: Optional[str] = None,
    main_filter: Optional[str] = None,
    sub_filter: Optional[str] = None,
    view_type: str = "logistics",
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT, UserRole.OUTLET_MANAGER
    ))
):
    """ Get daily order summary including volume, revenue, and quantity by status. """
    try:
        # Role-based outlet filtering
        current_user = await user_manager.fetch(current_user_id)
        target_outlet_id = outlet_id
        if current_user.role == UserRole.OUTLET_MANAGER:
            target_outlet_id = current_user.outlet_id

        async with engine.connect() as conn:
            if view_type == "marketing":
                summary = await fetch_marketing_order_summary(
                    conn, from_date, to_date, target_outlet_id, main_filter, sub_filter
                )
            else:
                summary = await fetch_logistics_order_summary(
                    conn, from_date, to_date, target_outlet_id, main_filter, sub_filter
                )

        return {
            "summary": summary,
            "filters_applied": {
                "from_date": from_date,
                "to_date": to_date,
                "outlet_id": target_outlet_id,
                "main_filter": main_filter,
                "sub_filter": sub_filter,
                "view_type": view_type
            }
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch daily order summary: {str(e)}"
        )


@router.get("/outlet-financial-summary")
async def get_outlet_financial_summary(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT
    ))
):
    """
    Per-outlet financial aggregation for the SuperAdmin dashboard.
    Replaces 51+ individual API calls with a single DB-side aggregation.
    """
    FINANCIAL_QUERY = text("""
    WITH delivered_orders AS (
        SELECT
            assigned_outlet_id,
            SUM(gross_amount)      AS revenue,
            SUM(discount_applied)  AS discounts,
            SUM(total_commission)  AS commission,
            SUM(prepaid_amount)    AS prepaid,
            SUM(total_amount)      AS collected_at_outlet,
            COUNT(*)               AS order_count
        FROM customer_orders
        WHERE order_status = 'DELIVERED'
          AND assigned_outlet_id IS NOT NULL
          AND (CAST(:from_date AS DATE) IS NULL OR (actual_delivery_date AT TIME ZONE 'Asia/Kolkata')::date >= CAST(:from_date AS DATE))
          AND (CAST(:to_date AS DATE) IS NULL OR (actual_delivery_date AT TIME ZONE 'Asia/Kolkata')::date <= CAST(:to_date AS DATE))
        GROUP BY assigned_outlet_id
    ),
    all_orders AS (
        SELECT assigned_outlet_id, COUNT(*) AS all_order_count
        FROM customer_orders
        WHERE assigned_outlet_id IS NOT NULL
        GROUP BY assigned_outlet_id
    ),
    collections AS (
        SELECT outlet_id, SUM(amount) AS collections
        FROM outlet_daily_collections
        WHERE confirmation_status = 'CONFIRMED'
          AND (CAST(:from_date AS DATE) IS NULL OR (confirmed_at AT TIME ZONE 'Asia/Kolkata')::date >= CAST(:from_date AS DATE))
          AND (CAST(:to_date AS DATE) IS NULL OR (confirmed_at AT TIME ZONE 'Asia/Kolkata')::date <= CAST(:to_date AS DATE))
        GROUP BY outlet_id
    )
    SELECT
        o.uid,
        o.outlet_name,
        o.outlet_code,
        o.is_active,
        COALESCE(d.revenue, 0)             AS revenue,
        COALESCE(d.discounts, 0)           AS discounts,
        COALESCE(d.commission, 0)          AS commission,
        COALESCE(d.prepaid, 0)             AS prepaid,
        COALESCE(d.collected_at_outlet, 0) AS collected_at_outlet,
        COALESCE(c.collections, 0)         AS collections,
        COALESCE(d.order_count, 0)         AS order_count,
        COALESCE(a.all_order_count, 0)     AS all_order_count
    FROM outlets o
    LEFT JOIN delivered_orders d ON o.uid = d.assigned_outlet_id
    LEFT JOIN all_orders a       ON o.uid = a.assigned_outlet_id
    LEFT JOIN collections c      ON o.uid = c.outlet_id
    ORDER BY o.outlet_name
    """)

    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                FINANCIAL_QUERY,
                {"from_date": from_date,
                 "to_date":   to_date}
            )
            rows = result.all()

        outlets = []
        totals = {
            "revenue": 0.0, "discounts": 0.0, "commission": 0.0,
            "prepaid": 0.0, "collected_at_outlet": 0.0, "collections": 0.0,
            "outstanding": 0.0, "net_revenue": 0.0,
            "total_orders": 0, "delivered_orders": 0,
        }

        for row in rows:
            revenue            = float(row.revenue)
            discounts          = float(row.discounts)
            commission         = float(row.commission)
            prepaid            = float(row.prepaid)
            collected_at_outlet = float(row.collected_at_outlet)
            collections        = float(row.collections)
            order_count        = int(row.order_count)
            all_order_count    = int(row.all_order_count)
            outstanding        = collected_at_outlet - collections
            net_revenue        = prepaid + collected_at_outlet

            outlets.append({
                "uid": row.uid,
                "outlet_name": row.outlet_name,
                "outlet_code": row.outlet_code,
                "is_active": row.is_active,
                "summary": {
                    "revenue": revenue,
                    "discounts": discounts,
                    "commission": commission,
                    "prepaid": prepaid,
                    "collected_at_outlet": collected_at_outlet,
                    "collections": collections,
                    "outstanding": outstanding,
                    "net_revenue": net_revenue,
                    "order_count": order_count,
                    "all_order_count": all_order_count,
                },
            })

            totals["revenue"]             += revenue
            totals["discounts"]           += discounts
            totals["commission"]          += commission
            totals["prepaid"]             += prepaid
            totals["collected_at_outlet"] += collected_at_outlet
            totals["collections"]         += collections
            totals["total_orders"]        += all_order_count
            totals["delivered_orders"]    += order_count

        totals["outstanding"]  = totals["collected_at_outlet"] - totals["collections"]
        totals["net_revenue"]  = totals["prepaid"] + totals["collected_at_outlet"]

        return {"totals": totals, "outlets": outlets}

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch outlet financial summary: {str(e)}"
        )


@router.get("/daily-collection-tracker")
async def get_daily_collection_tracker(
    from_date: date,
    to_date: date,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT
    ))
):
    """
    Date-wise delivery-vs-collection breakdown per outlet.

    For each outlet×date combination that has either a delivered order or a
    confirmed collection within the requested window, returns:
      - to_collect  : sum of total_amount from DELIVERED orders (actual_delivery_date)
      - paid        : sum of amount from CONFIRMED outlet collections (collection date)
      - remaining   : to_collect − paid
      - order_count : number of delivered orders on that date
    """
    QUERY = text("""
    WITH delivery_data AS (
        SELECT
            co.assigned_outlet_id,
            (co.actual_delivery_date AT TIME ZONE 'Asia/Kolkata')::date AS activity_date,
            SUM(co.total_amount) AS to_collect,
            COUNT(co.uid)        AS order_count
        FROM customer_orders co
        WHERE co.order_status = 'DELIVERED'
          AND co.assigned_outlet_id IS NOT NULL
          AND co.actual_delivery_date IS NOT NULL
          AND (co.actual_delivery_date AT TIME ZONE 'Asia/Kolkata')::date
                  BETWEEN :from_date AND :to_date
        GROUP BY co.assigned_outlet_id, 2
    ),
    collection_data AS (
        SELECT
            odc.outlet_id,
            odc.date AS activity_date,
            SUM(odc.amount) AS paid
        FROM outlet_daily_collections odc
        WHERE odc.confirmation_status = 'CONFIRMED'
          AND odc.date BETWEEN :from_date AND :to_date
        GROUP BY odc.outlet_id, odc.date
    ),
    combined AS (
        SELECT
            COALESCE(d.assigned_outlet_id, c.outlet_id) AS outlet_id,
            COALESCE(d.activity_date, c.activity_date)  AS activity_date,
            COALESCE(d.to_collect, 0)  AS to_collect,
            COALESCE(d.order_count, 0) AS order_count,
            COALESCE(c.paid, 0)        AS paid
        FROM delivery_data d
        FULL OUTER JOIN collection_data c
            ON d.assigned_outlet_id = c.outlet_id
           AND d.activity_date = c.activity_date
    )
    SELECT
        cmb.outlet_id,
        o.outlet_name,
        o.outlet_code,
        cmb.activity_date AS date,
        cmb.to_collect,
        cmb.paid,
        cmb.to_collect - cmb.paid AS remaining,
        cmb.order_count
    FROM combined cmb
    JOIN outlets o ON o.uid = cmb.outlet_id
    ORDER BY cmb.activity_date, o.outlet_name
    """)

    try:
        async with engine.connect() as conn:
            result = await conn.execute(QUERY, {"from_date": from_date, "to_date": to_date})
            rows = result.all()

        daily_data = []
        outlet_map = {}   # outlet_id → running aggregate
        grand = {"to_collect": 0.0, "paid": 0.0, "remaining": 0.0}

        for row in rows:
            tc  = float(row.to_collect)
            pd_ = float(row.paid)
            rm  = float(row.remaining)

            daily_data.append({
                "outlet_id":   row.outlet_id,
                "outlet_name": row.outlet_name,
                "outlet_code": row.outlet_code,
                "date":        row.date.isoformat(),
                "to_collect":  tc,
                "paid":        pd_,
                "remaining":   rm,
                "order_count": int(row.order_count),
            })

            if row.outlet_id not in outlet_map:
                outlet_map[row.outlet_id] = {
                    "outlet_id":        row.outlet_id,
                    "outlet_name":      row.outlet_name,
                    "outlet_code":      row.outlet_code,
                    "total_to_collect": 0.0,
                    "total_paid":       0.0,
                    "total_remaining":  0.0,
                }
            outlet_map[row.outlet_id]["total_to_collect"] += tc
            outlet_map[row.outlet_id]["total_paid"]       += pd_
            outlet_map[row.outlet_id]["total_remaining"]  += rm

            grand["to_collect"] += tc
            grand["paid"]       += pd_
            grand["remaining"]  += rm

        return {
            "daily_data":       daily_data,
            "outlet_summaries": list(outlet_map.values()),
            "grand_total":      grand,
            "filters": {
                "from_date": from_date.isoformat(),
                "to_date":   to_date.isoformat(),
            },
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch daily collection tracker: {str(e)}"
        )


@router.get("/inventory-pivot")
async def get_inventory_pivot(
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT
    ))
):
    """
    Inventory pivot: all active outlets × all active products with quantities.
    Replaces 3 parallel frontend calls + client-side matrix construction.
    """
    PIVOT_QUERY = text("""
    SELECT
        o.uid          AS outlet_uid,
        o.outlet_name,
        o.outlet_code,
        p.uid          AS product_uid,
        p.product_name,
        p.sku,
        COALESCE(i.quantity, 0) AS quantity
    FROM outlets o
    CROSS JOIN products p
    LEFT JOIN inventory i ON i.outlet_id = o.uid AND i.product_id = p.uid
    WHERE o.is_active = true AND p.is_active = true
    ORDER BY o.outlet_name, p.product_name
    """)

    try:
        async with engine.connect() as conn:
            result = await conn.execute(PIVOT_QUERY)
            rows = result.all()

        outlets_map = {}
        products_map = {}
        matrix: Dict[str, Dict[str, int]] = {}
        row_totals: Dict[str, int] = {}
        col_totals: Dict[str, int] = {}
        grand_total = 0

        for row in rows:
            oid = row.outlet_uid
            pid = row.product_uid
            qty = int(row.quantity)

            if oid not in outlets_map:
                outlets_map[oid] = {"uid": oid, "outlet_name": row.outlet_name, "outlet_code": row.outlet_code}
                matrix[oid] = {}
                row_totals[oid] = 0

            if pid not in products_map:
                products_map[pid] = {"uid": pid, "product_name": row.product_name, "sku": row.sku}
                col_totals[pid] = 0

            if qty > 0:
                matrix[oid][pid] = qty

            row_totals[oid] += qty
            col_totals[pid] += qty
            grand_total += qty

        return {
            "outlets": list(outlets_map.values()),
            "products": list(products_map.values()),
            "matrix": matrix,
            "row_totals": row_totals,
            "col_totals": col_totals,
            "grand_total": grand_total,
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch inventory pivot: {str(e)}"
        )

@router.get("/outlet-product-summary")
async def get_outlet_product_summary(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    outlet_id: Optional[str] = None,
    order_status: Optional[OrderStatus] = None,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.ACCOUNTANT, UserRole.OUTLET_MANAGER
    ))
):
    """
    Get product quantity and amount summary grouped by outlet, status, product and variant (SKU).
    """
    try:
        # Build filters
        filters = {}
        
        # Date range filtering (nested under 'order' relationship)
        date_filter = {}
        if from_date:
            date_filter["$gte"] = datetime.combine(from_date, datetime.min.time())
        if to_date:
            date_filter["$lte"] = datetime.combine(to_date, datetime.max.time())
        
        if date_filter:
            filters["order.order_date"] = date_filter
            
        # Role-based outlet filtering
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER:
            filters["order.assigned_outlet_id"] = current_user.outlet_id
        elif outlet_id:
            filters["order.assigned_outlet_id"] = outlet_id
            
        if order_status:
            filters["order.order_status"] = order_status
            
        # Fetch summary from manager
        summary = await order_item_manager.get_outlet_product_summary(filters=filters)
        
        return {
            "items": summary,
            "filters_applied": {
                "from_date": from_date,
                "to_date": to_date,
                "outlet_id": outlet_id if current_user.role != UserRole.OUTLET_MANAGER else current_user.outlet_id,
                "order_status": order_status
            }
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch outlet product summary: {str(e)}"
        )


@router.get("/product-quantity-report")
async def get_product_quantity_report(
    from_date: date,
    to_date: date,
    current_user_id: str = Depends(require_roles(UserRole.SUPER_ADMIN))
):
    """Product quantity report grouped by date, outlet, status, and product."""
    try:
        data = await order_item_manager.get_product_quantity_report(
            start_date=from_date,
            end_date=to_date
        )
        return {
            "items": data,
            "filters_applied": {"from_date": from_date, "to_date": to_date}
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch product quantity report: {str(e)}"
        )
