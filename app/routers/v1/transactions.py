from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional
from datetime import datetime, date
from decimal import Decimal

from config import get_settings, get_engine
from managers import (
    OrderTransactionManager, CustomerOrderManager, UserManager,
    OrderTransactionSchema
)
from models import (
    OrderTransactionCreateRequest, OrderTransactionResponse,
    ListResponse, StatusResponse
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, PaymentStatus, PaymentMethod

settings = get_settings()
engine = get_engine(settings.name)

transaction_manager = OrderTransactionManager(engine)
order_manager = CustomerOrderManager(engine)
user_manager = UserManager(engine)

router = APIRouter(prefix="/transactions", tags=["Payment Transactions"])


@router.post("", response_model=OrderTransactionResponse)
async def record_order_payment(
    payload: OrderTransactionCreateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER
    ))
):
    """
    Record payment transaction for an order
    Used when outlet manager receives payment on delivery
    """
    try:
        # Verify order exists and check access
        order = await order_manager.fetch(payload.order_id)
        
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER:
            if order.assigned_outlet_id != current_user.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this order"
                )
        
        # Check if order can receive payment
        if order.order_status not in [OrderStatus.DELIVERY_ALLOTTED, OrderStatus.DELIVERED]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Order must be assigned or delivered to record payment"
            )
        
        # Create transaction record
        transaction = OrderTransactionSchema(
            order_id=payload.order_id,
            payment_status=PaymentStatus.PAID,  # Recording actual payment received
            payment_method=payload.payment_method,
            amount_paid=payload.amount_paid,
            transaction_reference=payload.transaction_reference,
            payment_date=datetime.now(),
            received_by=current_user_id,
            notes=payload.notes
        )
        
        created_transaction = await transaction_manager.create(transaction)
        
        # Update order status to delivered if payment received
        await order_manager.update(payload.order_id, {
            "order_status": OrderStatus.DELIVERED,
            "actual_delivery_date": datetime.now()
        })
        
        return OrderTransactionResponse(
            uid=created_transaction.uid,
            order_id=created_transaction.order_id,
            payment_status=created_transaction.payment_status,
            payment_method=created_transaction.payment_method,
            amount_paid=created_transaction.amount_paid,
            transaction_reference=created_transaction.transaction_reference,
            payment_date=created_transaction.payment_date,
            received_by=created_transaction.received_by,
            notes=created_transaction.notes,
            created_at=created_transaction.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to record payment: {str(e)}"
        )


# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

# Duplicate /{transaction_id} route removed - moved to top of file
    """Get specific transaction details"""
    try:
        transaction = await transaction_manager.fetch(transaction_id)
        
        # Check access permissions
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER:
            # Verify the transaction belongs to user's outlet
            order = await order_manager.fetch(transaction.order_id)
            if order.outlet_id != current_user.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this transaction"
                )
        
        return OrderTransactionResponse(
            uid=transaction.uid,
            order_id=transaction.order_id,
            amount=transaction.amount,
            payment_method=transaction.payment_method,
            collection_type=transaction.collection_type,
            reference_number=transaction.reference_number,
            notes=transaction.notes,
            created_at=transaction.created_at,
            created_by=transaction.created_by
        )
    
    except Exception as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch transaction: {str(e)}"
        )


# Duplicate /order/{order_id} route removed - moved to top of file
    """Get all transactions for a specific order"""
    try:
        # Verify order exists and check access
        order = await order_manager.fetch(order_id)
        
        current_user = await user_manager.fetch(current_user_id)
        
        # Role-based access control
        if current_user.role == UserRole.TELECALLER:
            if order.telecaller_id != current_user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view transactions for your own orders"
                )
        elif current_user.role == UserRole.OUTLET_MANAGER:
            if order.outlet_id != current_user.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only view transactions for your outlet's orders"
                )
        
        # Get transactions
        transactions = await transaction_manager.fetch_all(
            filters={"order_id": order_id}
        )
        
        return [
            OrderTransactionResponse(
                uid=transaction.uid,
                order_id=transaction.order_id,
                amount=transaction.amount,
                payment_method=transaction.payment_method,
                collection_type=transaction.collection_type,
                reference_number=transaction.reference_number,
                notes=transaction.notes,
                created_at=transaction.created_at,
                created_by=transaction.created_by
            )
            for transaction in transactions.items
        ]
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch order transactions: {str(e)}"
        )


# Duplicate /daily-collection/{collection_date} route removed - moved to top of file
    """Get daily collection summary for a specific date"""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        # Build filters
        filters = {
            "created_at__date": collection_date
        }
        
        # Role-based filtering
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id:
                # Get transactions for orders from this outlet
                outlet_orders = await order_manager.fetch_all(
                    filters={"outlet_id": current_user.outlet_id}
                )
                order_ids = [order.uid for order in outlet_orders.items]
                if order_ids:
                    filters["order_id__in"] = order_ids
                else:
                    # No orders for this outlet
                    return {
                        "collection_date": collection_date.isoformat(),
                        "outlet_id": current_user.outlet_id,
                        "total_collection": 0.0,
                        "transaction_count": 0,
                        "payment_method_breakdown": {},
                        "transactions": []
                    }
        elif outlet_id and current_user.role in [UserRole.ACCOUNTANT, UserRole.ADMIN, UserRole.SUPER_ADMIN]:
            # Filter by specific outlet
            outlet_orders = await order_manager.fetch_all(
                filters={"outlet_id": outlet_id}
            )
            order_ids = [order.uid for order in outlet_orders.items]
            if order_ids:
                filters["order_id__in"] = order_ids
        
        # Get transactions
        transactions = await transaction_manager.fetch_all(filters=filters)
        
        # Calculate summary
        total_collection = Decimal('0')
        payment_method_breakdown = {}
        
        transaction_details = []
        for transaction in transactions.items:
            total_collection += transaction.amount
            
            # Payment method breakdown
            method = transaction.payment_method.value
            if method not in payment_method_breakdown:
                payment_method_breakdown[method] = {
                    "count": 0,
                    "amount": Decimal('0')
                }
            payment_method_breakdown[method]["count"] += 1
            payment_method_breakdown[method]["amount"] += transaction.amount
            
            transaction_details.append({
                "transaction_id": transaction.uid,
                "order_id": transaction.order_id,
                "amount": float(transaction.amount),
                "payment_method": method,
                "collection_type": transaction.collection_type.value,
                "reference_number": transaction.reference_number,
                "created_at": transaction.created_at.isoformat()
            })
        
        return {
            "collection_date": collection_date.isoformat(),
            "outlet_id": outlet_id,
            "total_collection": float(total_collection),
            "transaction_count": len(transactions.items),
            "payment_method_breakdown": {
                method: {
                    "count": data["count"],
                    "amount": float(data["amount"])
                }
                for method, data in payment_method_breakdown.items()
            },
            "transactions": transaction_details
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch daily collection: {str(e)}"
        )




@router.get("/daily-collection")
async def get_daily_collection(
    date: Optional[str] = None,  # Format: YYYY-MM-DD
    outlet_id: Optional[str] = None,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT))
):
    """Get daily payment collections"""
    try:
        from datetime import datetime, date as date_type
        
        # Parse date or use today
        if date:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        else:
            target_date = date_type.today()
        
        # Build filters
        filters = {}
        if outlet_id:
            filters["outlet_id"] = outlet_id
        
        # Get all transactions for the date
        all_transactions = await transaction_manager.fetch_all(filters=filters)
        
        # Filter by date
        daily_transactions = [
            t for t in all_transactions.items
            if t.payment_date.date() == target_date
        ]
        
        # Calculate totals by payment method
        totals = {}
        for transaction in daily_transactions:
            method = transaction.payment_method.value
            if method not in totals:
                totals[method] = {"count": 0, "amount": 0}
            totals[method]["count"] += 1
            totals[method]["amount"] += float(transaction.amount_paid)
        
        return {
            "date": target_date.isoformat(),
            "outlet_id": outlet_id,
            "transactions": daily_transactions,
            "summary": {
                "total_transactions": len(daily_transactions),
                "total_amount": sum(float(t.amount_paid) for t in daily_transactions),
                "by_payment_method": totals
            }
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch daily collection: {str(e)}"
        )


@router.get("/{order_id}", response_model=List[OrderTransactionResponse])
async def get_order_transactions(
    order_id: str,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.TELECALLER))
):
    """Get all transactions for an order"""
    try:
        transactions = await transaction_manager.fetch_all(filters={"order_id": order_id})
        return transactions.items
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transactions not found: {str(e)}"
        )


@router.put("/{transaction_id}", response_model=OrderTransactionResponse)
async def update_transaction(
    transaction_id: str,
    payload: PaymentStatusUpdateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER))
):
    """Update transaction payment status"""
    try:
        updates = {
            "payment_status": payload.payment_status,
            "notes": payload.notes
        }
        
        updated_transaction = await transaction_manager.update(transaction_id, updates)
        return updated_transaction
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to update transaction: {str(e)}"
        )


# GENERIC ROUTE LAST (after all specific routes)

@router.get("", response_model=ListResponse[OrderTransactionResponse])
async def get_payment_transactions(
    order_id: Optional[str] = None,
    payment_status: Optional[PaymentStatus] = None,
    payment_method: Optional[PaymentMethod] = None,
    received_by: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    limit: int = 50,
    offset: int = 0,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT
    ))
):
    """
    Get payment transaction history with filters
    Outlet managers can only see transactions from their outlet's orders
    """
    try:
        filters = {}
        
        # Build filters
        if order_id:
            filters["order_id"] = order_id
        if payment_status:
            filters["payment_status"] = payment_status
        if payment_method:
            filters["payment_method"] = payment_method
        if received_by:
            filters["received_by"] = received_by
        
        # Get transactions
        transactions = await transaction_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=filters if filters else None
        )
        
        # Filter by date and outlet access
        current_user = await user_manager.fetch(current_user_id)
        filtered_transactions = []
        
        for transaction in transactions.items:
            # Date filtering
            if from_date and transaction.payment_date and transaction.payment_date.date() < from_date:
                continue
            if to_date and transaction.payment_date and transaction.payment_date.date() > to_date:
                continue
            
            # Outlet manager access control
            if current_user.role == UserRole.OUTLET_MANAGER:
                # Get the order to check outlet
                order = await order_manager.fetch(transaction.order_id)
                if order.assigned_outlet_id != current_user.outlet_id:
                    continue
            
            filtered_transactions.append(OrderTransactionResponse(
                uid=transaction.uid,
                order_id=transaction.order_id,
                payment_status=transaction.payment_status,
                payment_method=transaction.payment_method,
                amount_paid=transaction.amount_paid,
                transaction_reference=transaction.transaction_reference,
                payment_date=transaction.payment_date,
                received_by=transaction.received_by,
                notes=transaction.notes,
                created_at=transaction.created_at
            ))
        
        return ListResponse(items=filtered_transactions, count=len(filtered_transactions))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch transactions: {str(e)}"
        )


# Duplicate /{transaction_id} route removed - moved to top of file
    """
    Get transaction details by ID
    """
    try:
        transaction = await transaction_manager.fetch(transaction_id)
        
        # Check outlet access for outlet managers
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER:
            order = await order_manager.fetch(transaction.order_id)
            if order.assigned_outlet_id != current_user.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this transaction"
                )
        
        return OrderTransactionResponse(
            uid=transaction.uid,
            order_id=transaction.order_id,
            payment_status=transaction.payment_status,
            payment_method=transaction.payment_method,
            amount_paid=transaction.amount_paid,
            transaction_reference=transaction.transaction_reference,
            payment_date=transaction.payment_date,
            received_by=transaction.received_by,
            notes=transaction.notes,
            created_at=transaction.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction not found: {str(e)}"
        )


# Duplicate /order/{order_id} route removed - moved to top of file
    """
    Get all transactions for a specific order
    """
    try:
        # Verify order exists and check access
        order = await order_manager.fetch(order_id)
        
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER:
            if order.assigned_outlet_id != current_user.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this order"
                )
        elif current_user.role == UserRole.TELECALLER:
            if order.telecaller_id != current_user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this order"
                )
        
        # Get transactions for this order
        transactions = await transaction_manager.fetch_all(
            filters={"order_id": order_id}
        )
        
        return [
            OrderTransactionResponse(
                uid=transaction.uid,
                order_id=transaction.order_id,
                payment_status=transaction.payment_status,
                payment_method=transaction.payment_method,
                amount_paid=transaction.amount_paid,
                transaction_reference=transaction.transaction_reference,
                payment_date=transaction.payment_date,
                received_by=transaction.received_by,
                notes=transaction.notes,
                created_at=transaction.created_at
            )
            for transaction in sorted(transactions.items, key=lambda x: x.created_at, reverse=True)
        ]
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch order transactions: {str(e)}"
        )


# Duplicate /daily-collection/{collection_date} route removed - moved to top of file
    """
    Get daily payment collection summary
    """
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        # Outlet manager can only see their outlet's collections
        if current_user.role == UserRole.OUTLET_MANAGER:
            outlet_id = current_user.outlet_id
        
        # Get all transactions for the date
        all_transactions = await transaction_manager.fetch_all()
        
        # Filter transactions
        filtered_transactions = []
        for transaction in all_transactions.items:
            if not transaction.payment_date or transaction.payment_date.date() != collection_date:
                continue
            
            # Filter by outlet if specified
            if outlet_id:
                order = await order_manager.fetch(transaction.order_id)
                if order.assigned_outlet_id != outlet_id:
                    continue
            
            filtered_transactions.append(transaction)
        
        # Calculate summary
        total_amount = sum([float(t.amount_paid) for t in filtered_transactions])
        transaction_count = len(filtered_transactions)
        
        # Group by payment method
        payment_methods = {}
        for transaction in filtered_transactions:
            method = transaction.payment_method.value
            payment_methods[method] = payment_methods.get(method, 0) + float(transaction.amount_paid)
        
        # Group by payment status
        payment_status = {}
        for transaction in filtered_transactions:
            status_val = transaction.payment_status.value
            payment_status[status_val] = payment_status.get(status_val, 0) + float(transaction.amount_paid)
        
        return {
            "date": collection_date.isoformat(),
            "outlet_id": outlet_id,
            "summary": {
                "total_amount": total_amount,
                "transaction_count": transaction_count,
                "average_transaction": total_amount / transaction_count if transaction_count > 0 else 0
            },
            "payment_methods": payment_methods,
            "payment_status": payment_status,
            "transactions": [
                {
                    "transaction_id": t.uid,
                    "order_id": t.order_id,
                    "amount_paid": float(t.amount_paid),
                    "payment_method": t.payment_method.value,
                    "payment_status": t.payment_status.value,
                    "payment_time": t.payment_date.isoformat() if t.payment_date else None
                }
                for t in sorted(filtered_transactions, key=lambda x: x.payment_date or x.created_at, reverse=True)
            ]
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch daily collection: {str(e)}"
        )