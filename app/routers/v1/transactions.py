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
    PaymentStatusUpdateRequest,
    ListResponse, StatusResponse
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, PaymentStatus, PaymentMethod, OrderStatus

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
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.TELECALLER
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
        elif current_user.role == UserRole.TELECALLER:
            if order.telecaller_id != current_user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: You can only record payments for your own orders"
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
            payment_status=payload.payment_status,  # Recording actual payment received
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

@router.get("/daily-collection")
async def get_daily_collection(
    date: Optional[str] = None,  # Format: YYYY-MM-DD
    outlet_id: Optional[str] = None,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT, UserRole.TELECALLER))
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
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.TELECALLER))
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
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT, UserRole.TELECALLER
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
            elif current_user.role == UserRole.TELECALLER:
                # Get the order to check telecaller
                order = await order_manager.fetch(transaction.order_id)
                if order.telecaller_id != current_user_id:
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