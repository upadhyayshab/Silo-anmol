from fastapi import APIRouter, HTTPException, Depends, status
from typing import Dict, Any, List
from datetime import datetime, date, timedelta
from decimal import Decimal

from config import get_settings, get_engine
from managers import (
    SalesInvoiceManager, CustomerOrderManager, InventoryManager,
    ProductManager, OutletManager, UserManager, StockTransferOrderManager
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, OrderStatus, TransferStatus, PaymentStatus

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

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/super-admin")
async def get_super_admin_dashboard(
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN))
):
    """
    Super Admin Dashboard - Complete system overview
    """
    try:
        today = date.today()
        month_start = today.replace(day=1)
        
        # Get all data for comprehensive overview
        all_orders = await order_manager.fetch_all()
        all_invoices = await invoice_manager.fetch_all(filters={"is_cancelled": False})
        all_outlets = await outlet_manager.fetch_all(filters={"is_active": True})
        all_users = await user_manager.fetch_all(filters={"is_active": True})
        all_inventory = await inventory_manager.fetch_all()
        
        # Calculate KPIs
        total_orders = len(all_orders.items)
        pending_orders = len([o for o in all_orders.items if o.order_status == OrderStatus.PENDING])
        delivered_orders = len([o for o in all_orders.items if o.order_status == OrderStatus.DELIVERED])
        
        total_revenue = sum([float(inv.total_amount) for inv in all_invoices.items])
        monthly_revenue = sum([
            float(inv.total_amount) for inv in all_invoices.items 
            if inv.invoice_date >= month_start
        ])
        
        # Low stock items
        low_stock_items = []
        for inv in all_inventory.items:
            product = await product_manager.fetch(inv.product_id)
            if inv.quantity <= product.min_stock_level:
                low_stock_items.append({
                    "product_id": product.uid,
                    "product_name": product.product_name,
                    "current_stock": inv.quantity,
                    "min_level": product.min_stock_level,
                    "outlet_id": inv.outlet_id
                })
        
        return {
            "overview": {
                "total_orders": total_orders,
                "pending_orders": pending_orders,
                "delivered_orders": delivered_orders,
                "total_revenue": total_revenue,
                "monthly_revenue": monthly_revenue,
                "active_outlets": len(all_outlets.items),
                "active_users": len(all_users.items),
                "low_stock_alerts": len(low_stock_items)
            },
            "recent_orders": [
                {
                    "order_id": order.uid,
                    "order_number": order.order_number,
                    "customer_name": order.customer_name,
                    "status": order.order_status.value,
                    "total_amount": float(order.total_amount),
                    "order_date": order.order_date.isoformat()
                }
                for order in sorted(all_orders.items, key=lambda x: x.created_at, reverse=True)[:10]
            ],
            "low_stock_alerts": low_stock_items[:10],
            "outlet_performance": [
                {
                    "outlet_id": outlet.uid,
                    "outlet_name": outlet.outlet_name,
                    "orders_count": len([o for o in all_orders.items if o.assigned_outlet_id == outlet.uid]),
                    "revenue": sum([
                        float(inv.total_amount) for inv in all_invoices.items 
                        if inv.outlet_id == outlet.uid
                    ])
                }
                for outlet in all_outlets.items
            ]
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch dashboard data: {str(e)}"
        )


@router.get("/warehouse-manager/{user_id}")
async def get_warehouse_manager_dashboard(
    user_id: str,
    current_user_id: str = Depends(require_roles(UserRole.WAREHOUSE_MANAGER, UserRole.SUPER_ADMIN))
):
    """
    Warehouse Manager Dashboard - Inventory and transfer focus
    """
    try:
        # Verify user access (warehouse managers can only see their own dashboard)
        if current_user_id != user_id:
            current_user = await user_manager.fetch(current_user_id)
            if current_user.role != UserRole.SUPER_ADMIN:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied"
                )
        
        # Get warehouse inventory (outlet_id = NULL)
        warehouse_inventory = await inventory_manager.fetch_all(
            filters={"outlet_id": None}
        )
        
        # Get pending transfer requests with proper joins to load items
        pending_transfers = await transfer_manager.fetch_all(
            filters={"status": TransferStatus.PENDING},
            joins=["items"]  # Eagerly load transfer items
        )
        
        # Get all products for stock analysis
        all_products = await product_manager.fetch_all()
        
        # Calculate stock levels
        total_products = len(all_products.items)
        low_stock_count = 0
        out_of_stock_count = 0
        low_stock_alerts = []
        
        for inv in warehouse_inventory.items:
            try:
                product = await product_manager.fetch(inv.product_id)
                if inv.quantity == 0:
                    out_of_stock_count += 1
                elif inv.quantity <= product.min_stock_level:
                    low_stock_count += 1
                    low_stock_alerts.append({
                        "product_id": inv.product_id,
                        "product_name": product.product_name,
                        "current_stock": inv.quantity,
                        "min_level": product.min_stock_level,
                        "reserved_stock": inv.reserved_quantity,
                        "available_stock": inv.quantity - inv.reserved_quantity
                    })
            except Exception as e:
                # Skip products that can't be fetched
                continue
        
        # Build pending transfers response with safe item count
        pending_transfers_data = []
        for transfer in pending_transfers.items:
            try:
                # Get outlet name safely
                outlet_name = "Unknown Outlet"
                if transfer.to_outlet_id:
                    try:
                        outlet = await outlet_manager.fetch(transfer.to_outlet_id)
                        outlet_name = outlet.outlet_name
                    except:
                        pass
                
                # Get requester name safely
                requester_name = "Unknown User"
                if transfer.requested_by:
                    try:
                        requester = await user_manager.fetch(transfer.requested_by)
                        requester_name = requester.full_name
                    except:
                        pass
                
                # Count items safely
                items_count = 0
                if hasattr(transfer, 'items') and transfer.items:
                    items_count = len(transfer.items)
                else:
                    # Fallback: count items manually
                    try:
                        transfer_items = await transfer_item_manager.fetch_all(
                            filters={"transfer_id": transfer.uid}
                        )
                        items_count = len(transfer_items.items)
                    except:
                        items_count = 0
                
                pending_transfers_data.append({
                    "transfer_id": transfer.uid,
                    "to_outlet_id": transfer.to_outlet_id,
                    "to_outlet_name": outlet_name,
                    "requested_by": transfer.requested_by,
                    "requester_name": requester_name,
                    "scheduled_date": transfer.scheduled_date.isoformat() if transfer.scheduled_date else None,
                    "items_count": items_count,
                    "notes": transfer.notes,
                    "created_at": transfer.created_at.isoformat()
                })
            except Exception as e:
                # Skip transfers that can't be processed
                continue
        
        return {
            "inventory_overview": {
                "total_products": total_products,
                "low_stock_items": low_stock_count,
                "out_of_stock_items": out_of_stock_count,
                "pending_transfers": len(pending_transfers.items)
            },
            "pending_transfers": pending_transfers_data[:10],
            "low_stock_alerts": low_stock_alerts[:10]
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch dashboard data: {str(e)}"
        )


@router.get("/outlet-manager/{outlet_id}")
async def get_outlet_manager_dashboard(
    outlet_id: str,
    current_user_id: str = Depends(require_roles(UserRole.OUTLET_MANAGER, UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    Outlet Manager Dashboard - Outlet-specific operations
    """
    try:
        # Verify outlet access
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER and current_user.outlet_id != outlet_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this outlet"
            )
        
        today = date.today()
        
        # Get outlet-specific data
        outlet_orders = await order_manager.fetch_all(
            filters={"assigned_outlet_id": outlet_id}
        )
        outlet_invoices = await invoice_manager.fetch_all(
            filters={"outlet_id": outlet_id, "is_cancelled": False}
        )
        outlet_inventory = await inventory_manager.fetch_all(
            filters={"outlet_id": outlet_id}
        )
        
        # Today's data
        today_orders = [o for o in outlet_orders.items if o.order_date.date() == today]
        today_invoices = [i for i in outlet_invoices.items if i.invoice_date == today]
        
        # Calculate metrics
        pending_deliveries = len([o for o in outlet_orders.items if o.order_status == OrderStatus.DELIVERY_ALLOTTED])
        today_revenue = sum([float(inv.total_amount) for inv in today_invoices])
        
        return {
            "outlet_overview": {
                "assigned_orders": len(outlet_orders.items),
                "pending_deliveries": pending_deliveries,
                "today_orders": len(today_orders),
                "today_revenue": today_revenue,
                "total_inventory_items": len(outlet_inventory.items)
            },
            "assigned_orders": [
                {
                    "order_id": order.uid,
                    "order_number": order.order_number,
                    "customer_name": order.customer_name,
                    "customer_phone": order.customer_phone,
                    "status": order.order_status.value,
                    "total_amount": float(order.total_amount),
                    "expected_delivery": order.expected_delivery_date.isoformat() if order.expected_delivery_date else None
                }
                for order in sorted(outlet_orders.items, key=lambda x: x.created_at, reverse=True)[:10]
            ],
            "today_sales": [
                {
                    "invoice_id": inv.uid,
                    "invoice_number": inv.invoice_number,
                    "customer_name": inv.customer_name,
                    "total_amount": float(inv.total_amount),
                    "payment_method": inv.payment_method.value
                }
                for inv in today_invoices
            ],
            "inventory_status": [
                {
                    "product_id": inv.product_id,
                    "quantity": inv.quantity,
                    "reserved_quantity": inv.reserved_quantity,
                    "available_quantity": inv.quantity - inv.reserved_quantity
                }
                for inv in outlet_inventory.items
            ][:10]
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch dashboard data: {str(e)}"
        )


@router.get("/telecaller/{user_id}")
async def get_telecaller_dashboard(
    user_id: str,
    current_user_id: str = Depends(require_roles(UserRole.TELECALLER, UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    Telecaller Dashboard - Personal performance and orders
    """
    try:
        # Verify user access
        if current_user_id != user_id:
            current_user = await user_manager.fetch(current_user_id)
            if current_user.role not in [UserRole.SUPER_ADMIN, UserRole.ADMIN]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied"
                )
        
        today = date.today()
        month_start = today.replace(day=1)
        
        # Get telecaller's orders
        my_orders = await order_manager.fetch_all(
            filters={"telecaller_id": user_id}
        )
        
        # Calculate performance metrics
        total_orders = len(my_orders.items)
        today_orders = len([o for o in my_orders.items if o.order_date.date() == today])
        month_orders = len([o for o in my_orders.items if o.order_date.date() >= month_start])
        delivered_orders = len([o for o in my_orders.items if o.order_status == OrderStatus.DELIVERED])
        
        total_revenue = sum([float(o.total_amount) for o in my_orders.items if o.order_status == OrderStatus.DELIVERED])
        month_revenue = sum([
            float(o.total_amount) for o in my_orders.items 
            if o.order_status == OrderStatus.DELIVERED and o.order_date.date() >= month_start
        ])
        
        return {
            "performance": {
                "total_orders": total_orders,
                "today_orders": today_orders,
                "month_orders": month_orders,
                "delivered_orders": delivered_orders,
                "delivery_rate": (delivered_orders / total_orders * 100) if total_orders > 0 else 0,
                "total_revenue": total_revenue,
                "month_revenue": month_revenue
            },
            "recent_orders": [
                {
                    "order_id": order.uid,
                    "order_number": order.order_number,
                    "customer_name": order.customer_name,
                    "customer_phone": order.customer_phone,
                    "status": order.order_status.value,
                    "total_amount": float(order.total_amount),
                    "order_date": order.order_date.isoformat()
                }
                for order in sorted(my_orders.items, key=lambda x: x.created_at, reverse=True)[:10]
            ],
            "status_breakdown": {
                "pending": len([o for o in my_orders.items if o.order_status == OrderStatus.PENDING]),
                "delivery_allotted": len([o for o in my_orders.items if o.order_status == OrderStatus.DELIVERY_ALLOTTED]),
                "delivered": len([o for o in my_orders.items if o.order_status == OrderStatus.DELIVERED]),
                "cancelled": len([o for o in my_orders.items if o.order_status == OrderStatus.CANCELLED])
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch dashboard data: {str(e)}"
        )


@router.get("/accountant")
async def get_accountant_dashboard(
    _: str = Depends(require_roles(UserRole.ACCOUNTANT, UserRole.SUPER_ADMIN))
):
    """
    Accountant Dashboard - Financial overview and reports
    """
    try:
        today = date.today()
        month_start = today.replace(day=1)
        
        # Get financial data
        all_invoices = await invoice_manager.fetch_all(filters={"is_cancelled": False})
        all_orders = await order_manager.fetch_all()
        
        # Calculate financial metrics
        total_revenue = sum([float(inv.total_amount) for inv in all_invoices.items])
        monthly_revenue = sum([
            float(inv.total_amount) for inv in all_invoices.items 
            if inv.invoice_date >= month_start
        ])
        
        total_tax_collected = sum([float(inv.total_tax) for inv in all_invoices.items])
        monthly_tax = sum([
            float(inv.total_tax) for inv in all_invoices.items 
            if inv.invoice_date >= month_start
        ])
        
        # GST breakdown
        cgst_total = sum([float(inv.cgst_amount) for inv in all_invoices.items])
        sgst_total = sum([float(inv.sgst_amount) for inv in all_invoices.items])
        igst_total = sum([float(inv.igst_amount) for inv in all_invoices.items])
        
        # Payment method breakdown
        payment_methods = {}
        for inv in all_invoices.items:
            method = inv.payment_method.value
            payment_methods[method] = payment_methods.get(method, 0) + float(inv.total_amount)
        
        return {
            "financial_overview": {
                "total_revenue": total_revenue,
                "monthly_revenue": monthly_revenue,
                "total_tax_collected": total_tax_collected,
                "monthly_tax": monthly_tax,
                "total_invoices": len(all_invoices.items),
                "monthly_invoices": len([i for i in all_invoices.items if i.invoice_date >= month_start])
            },
            "gst_breakdown": {
                "cgst_collected": cgst_total,
                "sgst_collected": sgst_total,
                "igst_collected": igst_total,
                "total_gst": cgst_total + sgst_total + igst_total
            },
            "payment_methods": payment_methods,
            "recent_transactions": [
                {
                    "invoice_id": inv.uid,
                    "invoice_number": inv.invoice_number,
                    "customer_name": inv.customer_name,
                    "total_amount": float(inv.total_amount),
                    "tax_amount": float(inv.total_tax),
                    "payment_method": inv.payment_method.value,
                    "invoice_date": inv.invoice_date.isoformat()
                }
                for inv in sorted(all_invoices.items, key=lambda x: x.created_at, reverse=True)[:10]
            ]
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch dashboard data: {str(e)}"
        )