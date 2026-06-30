from fastapi import APIRouter, HTTPException, Depends, status
import sqlalchemy as db
from typing import Dict, Any, List
from datetime import datetime, date, timedelta
from decimal import Decimal
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from collections import defaultdict
from datetime import datetime, time

from config import get_settings, get_engine
from managers import (
    SalesInvoiceManager, CustomerOrderManager, InventoryManager,
    ProductManager, OutletManager, UserManager, StockTransferOrderManager,
    TransferItemManager, CustomerOrderSchema, OutletSchema, ProductSchema, InventorySchema
)
from utils.auth import require_permission, get_auth_context, AuthContext
from utils.permissions import Permission, ScopeLevel
from utils.constants import OrderStatus, TransferStatus, PaymentStatus, OutletType
from utils.warehouse_utils import get_default_warehouse_id

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
transfer_item_manager = TransferItemManager(engine)

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/super-admin")
async def get_super_admin_dashboard(
    ctx: AuthContext = Depends(require_permission(Permission.REPORTS_READ))
):
    """
    Super Admin Dashboard - Complete system overview
    """
    try:
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value):
            raise HTTPException(403, "Org-wide dashboard requires global scope")
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

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch dashboard data: {str(e)}"
        )



@router.get("/super-admin/orders-geography")
async def get_orders_geography_overview(
    district: str = None,
    taluk: str = None,
    from_date: str = None,
    to_date: str = None,
    ctx: AuthContext = Depends(require_permission(Permission.REPORTS_READ))
):
    """
    Super Admin - Orders geography overview
    Consolidated order counts grouped by District and Taluk with filters.
    Optimized via SQL aggregation.
    """
    try:
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value):
            raise HTTPException(403, "Org-wide dashboard requires global scope")
        # Parse date filters
        filter_from_date = None
        filter_to_date = None

        if from_date:
            try:
                filter_from_date = datetime.strptime(from_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid from_date format. Use YYYY-MM-DD")
        if to_date:
            try:
                filter_to_date = datetime.strptime(to_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid to_date format. Use YYYY-MM-DD")

        # Open an async database session for raw SQLAlchemy execution
        async with AsyncSession(engine) as session:
            query = (
                select(
                    CustomerOrderSchema.district,
                    CustomerOrderSchema.taluk,
                    CustomerOrderSchema.order_status,
                    func.count(CustomerOrderSchema.uid).label("order_count")
                )
                .select_from(CustomerOrderSchema)
            )

            # Apply District and Taluk filters directly to SQL
            if district:
                query = query.where(CustomerOrderSchema.district == district)
            if taluk:
                query = query.where(CustomerOrderSchema.taluk == taluk)
                
            # Apply Date filters directly to SQL
            if filter_from_date:
                start_dt = datetime.combine(filter_from_date, time.min)
                query = query.where(CustomerOrderSchema.order_date >= start_dt)
            if filter_to_date:
                end_dt = datetime.combine(filter_to_date, time.max)
                query = query.where(CustomerOrderSchema.order_date <= end_dt)

            # Group the results in the database
            query = query.group_by(
                CustomerOrderSchema.district,
                CustomerOrderSchema.taluk,
                CustomerOrderSchema.order_status
            )

            result = await session.execute(query)
            rows = result.all()

        # Initialize tracking variables
        total_orders = 0
        overall_status_counts = defaultdict(int)
        geography_data = {}

        # Loop through the pre-calculated rows
        for row in rows:
            dist = row.district or "Unknown District"
            t_name = row.taluk or "Unknown Taluk"
            order_status_val = row.order_status.value
            count = row.order_count

            # Update overall totals
            total_orders += count
            overall_status_counts[order_status_val] += count

            # Initialize district if not present
            if dist not in geography_data:
                geography_data[dist] = {
                    "total_orders": 0,
                    "status_breakdown": defaultdict(int),
                    "taluks": {}
                }
            
            # Initialize taluk if not present
            if t_name not in geography_data[dist]["taluks"]:
                geography_data[dist]["taluks"][t_name] = {
                    "total_orders": 0,
                    "status_breakdown": defaultdict(int)
                }

            # Increment counters
            geography_data[dist]["total_orders"] += count
            geography_data[dist]["status_breakdown"][order_status_val] += count
            
            geography_data[dist]["taluks"][t_name]["total_orders"] += count
            geography_data[dist]["taluks"][t_name]["status_breakdown"][order_status_val] += count

        return {
            "filters": {
                "district": district,
                "taluk": taluk,
                "from_date": from_date,
                "to_date": to_date
            },
            "summary": {
                "total_orders": total_orders,
                "overall_status_breakdown": dict(overall_status_counts)
            },
            "geography_breakdown": {
                dist: {
                    "total_orders": data["total_orders"],
                    "status_breakdown": dict(data["status_breakdown"]),
                    "taluks": {
                        t_name: {
                            "total_orders": t_data["total_orders"],
                            "status_breakdown": dict(t_data["status_breakdown"])
                        } for t_name, t_data in data["taluks"].items()
                    }
                } for dist, data in geography_data.items()
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch orders geography data: {str(e)}"
        )

@router.get("/super-admin/inventory-overview")
async def get_inventory_overview(
    product_id: str = None,
    outlet_id: str = None,
    from_date: str = None,
    to_date: str = None,
    ctx: AuthContext = Depends(require_permission(Permission.REPORTS_READ))
):
    """
    Super Admin - Consolidated Inventory Overview
    Optimized SQL query to fetch inventory across outlets with product, outlet, and date filters.
    """
    try:
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value):
            raise HTTPException(403, "Org-wide dashboard requires global scope")
        # Parse date filters
        filter_from_date = None
        filter_to_date = None

        if from_date:
            try:
                filter_from_date = datetime.strptime(from_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid from_date format. Use YYYY-MM-DD")
        if to_date:
            try:
                filter_to_date = datetime.strptime(to_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid to_date format. Use YYYY-MM-DD")

        # Open an async database session for raw SQLAlchemy execution
        async with AsyncSession(engine) as session:
            query = (
                select(
                    InventorySchema.product_id,
                    InventorySchema.outlet_id,
                    InventorySchema.quantity,
                    InventorySchema.last_updated,
                    ProductSchema.product_name,
                    ProductSchema.sku,
                    OutletSchema.outlet_name
                )
                .select_from(InventorySchema)
                # Join to get Product and Outlet names directly in the SQL result
                .join(ProductSchema, InventorySchema.product_id == ProductSchema.uid)
                .outerjoin(OutletSchema, InventorySchema.outlet_id == OutletSchema.uid)
            )

            # Apply Product Filter
            if product_id:
                query = query.where(InventorySchema.product_id == product_id)
            
            # Apply Outlet Filter
            if outlet_id:
                if outlet_id.lower() == "warehouse":
                    warehouse_id = await get_default_warehouse_id(engine)
                    query = query.where(
                        db.or_(
                            InventorySchema.outlet_id.is_(None),
                            InventorySchema.outlet_id == warehouse_id
                        )
                    )
                else:
                    query = query.where(InventorySchema.outlet_id == outlet_id)

            # Apply Date Filters on last_updated
            if filter_from_date:
                start_dt = datetime.combine(filter_from_date, time.min)
                query = query.where(InventorySchema.last_updated >= start_dt)
            if filter_to_date:
                end_dt = datetime.combine(filter_to_date, time.max)
                query = query.where(InventorySchema.last_updated <= end_dt)

            # Execute the optimized query
            result = await session.execute(query)
            rows = result.all()

        # Format the result into the expected JSON structure
        product_consolidation = {}
        total_global_quantity = 0

        for row in rows:
            pid = row.product_id
            qty = row.quantity or 0
            
            # Initialize product in dictionary if not present
            if pid not in product_consolidation:
                product_consolidation[pid] = {
                    "product_id": pid,
                    "product_name": row.product_name or "Unknown Product",
                    "sku": row.sku or "N/A",
                    "total_quantity": 0,
                    "warehouse_quantity": 0,
                    "outlets": []
                }

            # Increment totals
            total_global_quantity += qty
            product_consolidation[pid]["total_quantity"] += qty

            # Warehouse pool: NULL rows (legacy) + warehouse-type outlets
            outlet_id_str = str(row.outlet_id) if row.outlet_id else None
            warehouse_id = await get_default_warehouse_id(engine)

            if outlet_id_str is None or outlet_id_str == warehouse_id:
                product_consolidation[pid]["warehouse_quantity"] += qty
            else:
                product_consolidation[pid]["outlets"].append({
                    "outlet_id": row.outlet_id,
                    "outlet_name": row.outlet_name or "Unknown Outlet",
                    "quantity": qty,
                    "last_updated": row.last_updated.isoformat() if row.last_updated else None
                })

        return {
            "filters": {
                "product_id": product_id,
                "outlet_id": outlet_id,
                "from_date": from_date,
                "to_date": to_date
            },
            "overview": {
                "total_unique_products": len(product_consolidation),
                "total_global_quantity": total_global_quantity
            },
            "product_inventory": list(product_consolidation.values())
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch optimized inventory overview data: {str(e)}"
        )


@router.get("/super-admin/district-outlets-overview")
async def get_district_outlets_overview(
    from_date: str = None,
    to_date: str = None,
    ctx: AuthContext = Depends(require_permission(Permission.REPORTS_READ))
):
    """
    Super Admin - Optimized District-wise Outlet Overview
    Total order counts and revenue executed directly via SQL Aggregation.
    """
    try:
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value):
            raise HTTPException(403, "Org-wide dashboard requires global scope")
        # Parse date filters
        filter_from_date = None
        filter_to_date = None

        if from_date:
            try:
                filter_from_date = datetime.strptime(from_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid from_date format. Use YYYY-MM-DD")
        if to_date:
            try:
                filter_to_date = datetime.strptime(to_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid to_date format. Use YYYY-MM-DD")

        # Open an async database session for raw SQLAlchemy execution
        async with AsyncSession(engine) as session:
            # Build the optimized SQL query
            query = (
                select(
                    CustomerOrderSchema.district,
                    CustomerOrderSchema.assigned_outlet_id,
                    OutletSchema.outlet_name,
                    CustomerOrderSchema.order_status,
                    func.count(CustomerOrderSchema.uid).label("order_count"),
                    func.sum(CustomerOrderSchema.gross_amount - (CustomerOrderSchema.discount_applied + CustomerOrderSchema.manual_discount + CustomerOrderSchema.prepaid_amount)).label("revenue")
                )
                .select_from(CustomerOrderSchema)
                .outerjoin(OutletSchema, CustomerOrderSchema.assigned_outlet_id == OutletSchema.uid)
            )

            # Apply date filters directly to the SQL WHERE clause
            if filter_from_date:
                start_dt = datetime.combine(filter_from_date, time.min)
                query = query.where(CustomerOrderSchema.order_date >= start_dt)
            if filter_to_date:
                end_dt = datetime.combine(filter_to_date, time.max)
                query = query.where(CustomerOrderSchema.order_date <= end_dt)

            # Let PostgreSQL handle the heavy lifting of grouping
            query = query.group_by(
                CustomerOrderSchema.district,
                CustomerOrderSchema.assigned_outlet_id,
                OutletSchema.outlet_name,
                CustomerOrderSchema.order_status
            )

            # Execute the query
            result = await session.execute(query)
            rows = result.all()

        # Initialize summary metrics
        total_orders = 0
        overall_delivered_revenue = 0.0
        overall_status_counts = defaultdict(int)
        
        district_data = {}

        # Loop through the small set of pre-calculated rows
        for row in rows:
            dist = row.district or "Unknown District"
            oid = row.assigned_outlet_id
            outlet_name = row.outlet_name if row.outlet_name else "Unassigned"
            order_status_val = row.order_status.value
            count = row.order_count
            revenue = float(row.revenue) if row.revenue else 0.0

            # Update overall summary
            total_orders += count
            overall_status_counts[order_status_val] += count
            if order_status_val == OrderStatus.DELIVERED.value:
                overall_delivered_revenue += revenue

            # Build district hierarchy
            if dist not in district_data:
                district_data[dist] = {
                    "total_orders": 0,
                    "status_breakdown": defaultdict(int),
                    "outlets": {}
                }
            
            if outlet_name not in district_data[dist]["outlets"]:
                district_data[dist]["outlets"][outlet_name] = {
                    "outlet_id": oid,
                    "total_orders": 0,
                    "status_breakdown": defaultdict(int)
                }

            # Increment District level counters
            district_data[dist]["total_orders"] += count
            district_data[dist]["status_breakdown"][order_status_val] += count
            
            # Increment Outlet level counters (within the district)
            district_data[dist]["outlets"][outlet_name]["total_orders"] += count
            district_data[dist]["outlets"][outlet_name]["status_breakdown"][order_status_val] += count

        # Clean up defaultdicts for clean JSON serialization
        result_data = {}
        for dist, data in district_data.items():
            result_data[dist] = {
                "total_orders": data["total_orders"],
                "status_breakdown": dict(data["status_breakdown"]),
                "outlets": {
                    out_name: {
                        "outlet_id": out_data["outlet_id"],
                        "total_orders": out_data["total_orders"],
                        "status_breakdown": dict(out_data["status_breakdown"])
                    } for out_name, out_data in data["outlets"].items()
                }
            }

        return {
            "filters": {
                "from_date": from_date,
                "to_date": to_date
            },
            "summary": {
                "total_orders": total_orders,
                "overall_delivered_revenue": round(overall_delivered_revenue, 2),
                "overall_status_breakdown": dict(overall_status_counts)
            },
            "district_outlets_breakdown": result_data
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch optimized district outlets overview data: {str(e)}"
        )

@router.get("/warehouse-manager/{user_id}")
async def get_warehouse_manager_dashboard(
    user_id: str,
    ctx: AuthContext = Depends(get_auth_context)
):
    """
    Warehouse Manager Dashboard - Inventory and transfer focus
    """
    try:
        # Self-scope: a user may only see their own dashboard; GLOBAL scope sees any.
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value or ctx.user_id == user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied"
            )

        # Get warehouse inventory — includes NULL (legacy) + warehouse-type outlet rows
        warehouse_id = await get_default_warehouse_id(engine)
        all_inventory = await inventory_manager.fetch_all()
        warehouse_items = [
            inv for inv in all_inventory.items
            if inv.outlet_id is None or str(inv.outlet_id) == warehouse_id
        ]
        
        # Get pending transfer requests (no joins - fetch items separately)
        pending_transfers = await transfer_manager.fetch_all(
            filters={"status": TransferStatus.PENDING}
        )
        
        # Get all products for stock analysis
        all_products = await product_manager.fetch_all()
        
        # Calculate stock levels
        total_products = len(all_products.items)
        low_stock_count = 0
        out_of_stock_count = 0
        low_stock_alerts = []
        
        for inv in warehouse_items:
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
                "pending_transfers": len(pending_transfers.items),
                "warehouse_inventory_count": len(warehouse_items)
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
    ctx: AuthContext = Depends(get_auth_context)
):
    """
    Outlet Manager Dashboard - Outlet-specific operations
    """
    try:
        # Self-scope: a caller may only view their own outlet; GLOBAL scope sees any.
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value or ctx.outlet_id == outlet_id):
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
    from_date: str = None,
    to_date: str = None,
    ctx: AuthContext = Depends(get_auth_context)
):
    """
    Telecaller Dashboard - Personal performance and orders with date filters

    Query Parameters:
    - from_date: Filter orders from this date (format: YYYY-MM-DD)
    - to_date: Filter orders until this date (format: YYYY-MM-DD)
    """
    try:
        # Self-scope: a telecaller may only see their own dashboard; GLOBAL scope sees any.
        if not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value or ctx.user_id == user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied"
            )

        # Parse date filters
        filter_from_date = None
        filter_to_date = None
        
        if from_date:
            try:
                filter_from_date = datetime.strptime(from_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid from_date format. Use YYYY-MM-DD"
                )
        
        if to_date:
            try:
                filter_to_date = datetime.strptime(to_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid to_date format. Use YYYY-MM-DD"
                )
        
        today = date.today()
        month_start = today.replace(day=1)
        
        # Get telecaller's orders with items
        my_orders = await order_manager.fetch_all(
            filters={"telecaller_id": user_id}
        )
        
        # Fetch items and products for each order
        from managers import OrderItemManager, ProductManager
        order_item_manager = OrderItemManager(engine)
        product_manager_instance = ProductManager(engine)
        
        # Build a map of order items with product details
        order_items_map = {}
        product_cache = {}
        
        for order in my_orders.items:
            items = await order_item_manager.fetch_all(filters={"order_id": order.uid})
            order_items_with_products = []
            
            for item in items.items:
                # Fetch product details (with caching)
                if item.product_id not in product_cache:
                    try:
                        product_cache[item.product_id] = await product_manager_instance.fetch(item.product_id)
                    except:
                        product_cache[item.product_id] = None
                
                # Attach product to item
                item.product = product_cache[item.product_id]
                order_items_with_products.append(item)
            
            order_items_map[order.uid] = order_items_with_products
        
        # Apply date filters
        filtered_orders = my_orders.items
        if filter_from_date:
            filtered_orders = [o for o in filtered_orders if o.order_date.date() >= filter_from_date]
        if filter_to_date:
            filtered_orders = [o for o in filtered_orders if o.order_date.date() <= filter_to_date]
        
        # Calculate performance metrics
        total_orders = len(filtered_orders)
        today_orders = len([o for o in filtered_orders if o.order_date.date() == today])
        month_orders = len([o for o in filtered_orders if o.order_date.date() >= month_start])
        delivered_orders = len([o for o in filtered_orders if o.order_status == OrderStatus.DELIVERED])
        
        total_revenue = sum([float(o.total_amount) for o in filtered_orders if o.order_status == OrderStatus.DELIVERED])
        month_revenue = sum([
            float(o.total_amount) for o in filtered_orders 
            if o.order_status == OrderStatus.DELIVERED and o.order_date.date() >= month_start
        ])
        
        # Build product-wise summary
        product_summary = {}
        
        for order in filtered_orders:
            # Get items for this order from our map
            items_for_order = order_items_map.get(order.uid, [])
            
            for item in items_for_order:
                product_id = item.product_id
                
                if product_id not in product_summary:
                    product_summary[product_id] = {
                        "product_id": product_id,
                        "product_name": item.product.product_name if item.product else "Unknown",
                        "sku": item.product.sku if item.product else "N/A",
                        "total_quantity": 0,
                        "total_orders": 0,
                        "pending_orders": 0,
                        "delivery_allotted_orders": 0,
                        "delivered_orders": 0,
                        "cancelled_orders": 0,
                        "total_revenue": 0.0
                    }
                
                # Update quantities
                product_summary[product_id]["total_quantity"] += item.quantity
                product_summary[product_id]["total_orders"] += 1
                
                # Update status counts
                if order.order_status == OrderStatus.PENDING:
                    product_summary[product_id]["pending_orders"] += 1
                elif order.order_status == OrderStatus.DELIVERY_ALLOTTED:
                    product_summary[product_id]["delivery_allotted_orders"] += 1
                elif order.order_status == OrderStatus.DELIVERED:
                    product_summary[product_id]["delivered_orders"] += 1
                    product_summary[product_id]["total_revenue"] += float(item.subtotal)
                elif order.order_status == OrderStatus.CANCELLED:
                    product_summary[product_id]["cancelled_orders"] += 1
        
        # Build detailed orders list with product details
        detailed_orders = []
        for order in sorted(filtered_orders, key=lambda x: x.created_at, reverse=True):
            order_items = []
            
            # Get items for this order from our map
            items_for_order = order_items_map.get(order.uid, [])
            
            for item in items_for_order:
                order_items.append({
                    "item_id": item.uid,
                    "product_id": item.product_id,
                    "product_name": item.product.product_name if item.product else "Unknown",
                    "sku": item.product.sku if item.product else "N/A",
                    "hsn_code": item.product.hsn_code if item.product else "N/A",
                    "unit_of_measure": item.product.unit_of_measure.value if item.product and item.product.unit_of_measure else "N/A",
                    "quantity": item.quantity,
                    "unit_price": float(item.unit_price),
                    "product_manual_discount": float(item.product_manual_discount),
                    "subtotal": float(item.subtotal),
                    "tax_rate": float(item.tax_rate),
                    "tax_amount": float(item.tax_amount)
                })
            
            detailed_orders.append({
                "order_id": order.uid,
                "order_number": order.order_number,
                "customer_name": order.customer_name,
                "customer_phone": order.customer_phone,
                "address": {
                    "house_no": order.house_no,
                    "street": order.street,
                    "address_line": order.address_line,
                    "village": order.village,
                    "post": order.post,
                    "hobli": order.hobli,
                    "taluk": order.taluk,
                    "district": order.district,
                    "state": order.state,
                    "pincode": order.pincode
                },
                "status": order.order_status.value,
                "collection_type": order.collection_type.value,
                "payment_method": order.payment_method.value,
                "assigned_outlet_id": order.assigned_outlet_id,
                "order_date": order.order_date.isoformat(),
                "expected_delivery_date": order.expected_delivery_date.isoformat() if order.expected_delivery_date else None,
                "actual_delivery_date": order.actual_delivery_date.isoformat() if order.actual_delivery_date else None,
                "gross_amount": float(order.gross_amount),
                "discount_applied": float(order.discount_applied),
                "prepaid_amount": float(order.prepaid_amount),
                "total_amount": float(order.total_amount),
                "total_commission": float(order.total_commission),
                "status_remarks": order.status_remarks,
                "items": order_items
            })
        
        return {
            "filters": {
                "from_date": from_date,
                "to_date": to_date
            },
            "performance": {
                "total_orders": total_orders,
                "today_orders": today_orders,
                "month_orders": month_orders,
                "delivered_orders": delivered_orders,
                "delivery_rate": round((delivered_orders / total_orders * 100), 2) if total_orders > 0 else 0,
                "total_revenue": round(total_revenue, 2),
                "month_revenue": round(month_revenue, 2)
            },
            "orders": detailed_orders,
            "status_breakdown": {
                "pending": len([o for o in filtered_orders if o.order_status == OrderStatus.PENDING]),
                "delivery_allotted": len([o for o in filtered_orders if o.order_status == OrderStatus.DELIVERY_ALLOTTED]),
                "delivered": len([o for o in filtered_orders if o.order_status == OrderStatus.DELIVERED]),
                "cancelled": len([o for o in filtered_orders if o.order_status == OrderStatus.CANCELLED])
            },
            "product_summary": list(product_summary.values())
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
    ctx: AuthContext = Depends(require_permission(Permission.FINANCE_READ))
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