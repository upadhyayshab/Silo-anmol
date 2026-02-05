from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional, Dict, Any
from datetime import datetime, date, timedelta
from decimal import Decimal

from config import get_settings, get_engine
from managers import (
    SalesInvoiceManager, CustomerOrderManager, InventoryManager,
    ProductManager, OutletManager, UserManager, StockTransferOrderManager,
    ActivityLogManager
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, OrderStatus, TransferStatus, PaymentStatus
import calendar

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

router = APIRouter(prefix="/reports", tags=["Reports & Analytics"])


@router.get("/dashboard-overview")
async def get_outlet_orders(
    outlet_id: str,
    status: Optional[OrderStatus] = None,
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
        if status:
            filters["order_status"] = status
        
        # Fetch all orders for this outlet
        orders_result = await order_manager.fetch_all(
            filters=filters,
            limit=limit,
            offset=offset
        )
        
        # Process orders and apply date filtering
        order_list = []
        status_counts = {
            "pending": 0,
            "delivery_allotted": 0,
            "delivered": 0,
            "cancelled": 0
        }
        commission_summary = {
            "total_commission": 0.0,
            "by_status": {
                "pending": 0.0,
                "delivery_allotted": 0.0,
                "delivered": 0.0,
                "cancelled": 0.0
            }
        }
        
        for order in orders_result.items:
            # Apply date filtering if specified
            order_date = order.order_date.date()
            if from_date and order_date < from_date:
                continue
            if to_date and order_date > to_date:
                continue
            
            # Count status
            status_counts[order.order_status.value] += 1
            
            # Calculate commission (with backward compatibility)
            order_commission = float(getattr(order, 'total_commission', 0.0))
            commission_summary["total_commission"] += order_commission
            commission_summary["by_status"][order.order_status.value] += order_commission
            
            # Build order data
            order_data = {
                "order_id": order.uid,
                "order_number": order.order_number,
                "total_amount": float(order.total_amount),  # Final price after all calculations
                "status": order.order_status.value,  # OrderStatus enum value
                "customer_name": order.customer_name,
                "customer_phone": order.customer_phone,
                "order_date": order.order_date.isoformat(),
                "collection_type": order.collection_type.value,
                "gross_amount": float(order.gross_amount),
                "manual_discount": float(order.manual_discount),
                "discount_applied": float(order.discount_applied),
                "prepaid_amount": float(order.prepaid_amount),
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
                "status": status.value if status else None,
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
            if from_date <= order.order_date.date() <= to_date
        ]
        
        # Calculate metrics
        total_orders = len(period_orders)
        total_value = sum(float(order.total_amount) for order in period_orders)
        
        status_breakdown = {}
        for order in period_orders:
            status = order.order_status.value
            if status not in status_breakdown:
                status_breakdown[status] = {"count": 0, "value": 0}
            status_breakdown[status]["count"] += 1
            status_breakdown[status]["value"] += float(order.total_amount)
        
        # Delivery performance
        delivered_orders = [
            order for order in period_orders
            if order.order_status == OrderStatus.DELIVERED and order.actual_delivery_date
        ]
        
        avg_delivery_time = 0
        if delivered_orders:
            total_delivery_time = sum(
                (order.actual_delivery_date.date() - order.order_date.date()).days
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
    Get product performance analytics
    """
    try:
        if not from_date:
            from_date = date.today().replace(day=1)
        if not to_date:
            to_date = date.today()
        
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {"is_cancelled": False}
        if current_user.role == UserRole.OUTLET_MANAGER:
            filters["outlet_id"] = current_user.outlet_id
        elif outlet_id:
            filters["outlet_id"] = outlet_id
        
        invoices = await invoice_manager.fetch_all(filters=filters)
        
        # Filter by date range
        period_invoices = [
            inv for inv in invoices.items
            if from_date <= inv.invoice_date <= to_date
        ]
        
        # Get all invoice items for the period
        from managers import SalesInvoiceItemManager
        item_manager = SalesInvoiceItemManager(engine)
        
        product_performance = {}
        
        for inv in period_invoices:
            items = await item_manager.fetch_all(filters={"invoice_id": inv.uid})
            
            for item in items.items:
                if item.product_id not in product_performance:
                    product = await product_manager.fetch(item.product_id)
                    product_performance[item.product_id] = {
                        "product_name": product.product_name,
                        "sku": product.sku,
                        "quantity_sold": 0,
                        "revenue": 0,
                        "cost": 0,
                        "profit": 0,
                        "transactions": 0
                    }
                
                perf = product_performance[item.product_id]
                perf["quantity_sold"] += item.quantity
                perf["revenue"] += float(item.total_amount)
                perf["transactions"] += 1
                
                # Calculate cost and profit
                product = await product_manager.fetch(item.product_id)
                item_cost = float(product.cost_price) * item.quantity
                perf["cost"] += item_cost
                perf["profit"] += float(item.total_amount) - item_cost
        
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
            if from_date <= log.created_at.date() <= to_date
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
            if from_date <= transfer.created_at.date() <= to_date
        ]
        
        total_transfers = len(period_transfers)
        
        # Status breakdown
        status_counts = {}
        for transfer in period_transfers:
            status = transfer.status.value
            if status not in status_counts:
                status_counts[status] = 0
            status_counts[status] += 1
        
        # Delivery time analysis
        delivered_transfers = [
            t for t in period_transfers
            if t.status == TransferStatus.DELIVERED and t.delivered_date
        ]
        
        avg_delivery_time = 0
        if delivered_transfers:
            total_time = sum(
                (t.delivered_date.date() - t.created_at.date()).days
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