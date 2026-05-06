from fastapi import APIRouter, HTTPException, Depends, status, Body, BackgroundTasks
from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict
from decimal import Decimal
import re
from datetime import datetime, date, timedelta
from utils import dependencies as D
from config import get_settings, get_engine
from managers import (
    DeliveryGuyManager, UserManager, OutletManager, DeliveryGuySchema, UserSchema,
    CustomerOrderManager, OrderItemManager, OrderTransactionManager,
    InventoryManager, DeliveryTrackingManager, OrderTransactionSchema, DeliveryTrackingSchema,
    RateCardManager, RateCardSchema
)
from models import (
    DeliveryGuyCreateRequest, DeliveryGuyUpdateRequest, DeliveryGuyResponse,
    ListResponse, StatusResponse, UserResponse, OutletResponse
)
from utils.auth import require_roles, get_current_user_id, get_password_hash
from utils.constants import UserRole, OrderStatus, PaymentStatus, ActivityType, PaymentMethod
from utils.crm_utils import sync_order_to_crm

settings = get_settings()
engine = get_engine(settings.name)
delivery_guy_manager = DeliveryGuyManager(engine)
user_manager = UserManager(engine)
outlet_manager = OutletManager(engine)
order_manager = CustomerOrderManager(engine)
order_item_manager = OrderItemManager(engine)
transaction_manager = OrderTransactionManager(engine)
inventory_manager = InventoryManager(engine)
tracking_manager = DeliveryTrackingManager(engine)
rate_card_manager = RateCardManager(engine)

router = APIRouter(prefix="/delivery-guys", tags=["Delivery Guy Management"])

class DeliveryStatusUpdatePayload(BaseModel):
    order_id: str
    status: str = Field(..., description="delivered, postponed, attempted, cancelled")
    postpone_date: Optional[date] = None
    remarks: Optional[str] = None
    delivery_person_id: Optional[str] = None

@router.get("/{delivery_guy_id}", response_model=DeliveryGuyResponse)
async def get_delivery_guy(
    delivery_guy_id: str,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER, allowed_scopes=["delivery:read"]))
):
    try:
        delivery_guy = await delivery_guy_manager.fetch(delivery_guy_id)
        user = await user_manager.fetch(delivery_guy.user_id)
        outlet = await outlet_manager.fetch(delivery_guy.outlet_id)
        
        user_resp = UserResponse.from_orm(user)
        outlet_resp = OutletResponse.from_orm(outlet)
        
        return DeliveryGuyResponse(
            uid=delivery_guy.uid,
            user_id=delivery_guy.user_id,
            outlet_id=delivery_guy.outlet_id,
            is_active_for_delivery=delivery_guy.is_active_for_delivery,
            payout_frequency=delivery_guy.payout_frequency,
            is_deleted=delivery_guy.is_deleted,
            created_at=delivery_guy.created_at,
            user=user_resp,
            outlet=outlet_resp
        )
    except HTTPException:
        raise
    except Exception as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Delivery guy not found"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch delivery guy: {str(e)}"
        )

@router.get("", response_model=ListResponse[DeliveryGuyResponse])
async def list_delivery_guys(
    filters: Dict[str, Any] = Depends(D.filtering_dependency),
    sorts: List[str] = Depends(D.sorting_dependency),
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER, allowed_scopes=["delivery:read"]))
):
    try:
        delivery_guys = await delivery_guy_manager.fetch_all(
            limit=limit,
            offset=offset,
            sorts=sorts,
            filters=filters or None
        )
        
        responses = []
        for dg in delivery_guys.items:
            try:
                user = await user_manager.fetch(dg.user_id)
                outlet = await outlet_manager.fetch(dg.outlet_id)
                user_resp = UserResponse.from_orm(user)
                outlet_resp = OutletResponse.from_orm(outlet)
                
                responses.append(DeliveryGuyResponse(
                    uid=dg.uid,
                    user_id=dg.user_id,
                    outlet_id=dg.outlet_id,
                    is_active_for_delivery=dg.is_active_for_delivery,
                    payout_frequency=dg.payout_frequency,
                    is_deleted=dg.is_deleted,
                    created_at=dg.created_at,
                    user=user_resp,
                    outlet=outlet_resp
                ))
            except:
                continue

        return ListResponse(items=responses, count=len(responses))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch delivery guys: {str(e)}"
        )

@router.post("", response_model=DeliveryGuyResponse, status_code=status.HTTP_201_CREATED)
async def create_delivery_guy(
    payload: DeliveryGuyCreateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, allowed_scopes=["delivery:write"]))
):
    try:
        # 1. Use email from payload (frontend will send test emails)
        email = payload.email
        
        existing_phone = await user_manager.fetch_all(filters={"phone": payload.phone})
        if existing_phone.items:
             raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"User with phone {payload.phone} already exists"
            )
            
        # 3. Check if outlet exists
        outlet = await outlet_manager.fetch(payload.outlet_id)
        if not outlet.is_active:
             raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Outlet is not active"
            )

        # 4. Create User first
        # Password is always phone number as per requirements
        user = UserSchema(
            email=email,
            password_hash=get_password_hash(payload.phone),
            full_name=payload.full_name,
            role=UserRole.DELIVERY_GUY,
            phone=payload.phone,
            outlet_id=payload.outlet_id,
            is_active=True
        )
        created_user = await user_manager.create(user)

        # 5. Create Delivery Guy profile
        dg = DeliveryGuySchema(
            user_id=created_user.uid,
            outlet_id=payload.outlet_id,
            is_active_for_delivery=True,
            is_deleted=False
        )
        created_dg = await delivery_guy_manager.create(dg)
        
        user_resp = UserResponse.from_orm(created_user)
        outlet_resp = OutletResponse.from_orm(outlet)

        return DeliveryGuyResponse(
            uid=created_dg.uid,
            user_id=created_dg.user_id,
            outlet_id=created_dg.outlet_id,
            is_active_for_delivery=created_dg.is_active_for_delivery,
            payout_frequency=created_dg.payout_frequency,
            is_deleted=created_dg.is_deleted,
            created_at=created_dg.created_at,
            user=user_resp,
            outlet=outlet_resp
        )
    except HTTPException:
        raise
    except Exception as e:
        if "not found" in str(e).lower():
             raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Outlet not found"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create delivery guy: {str(e)}"
        )

@router.patch("/{delivery_guy_id}", response_model=DeliveryGuyResponse)
async def update_delivery_guy(
    delivery_guy_id: str,
    payload: DeliveryGuyUpdateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, allowed_scopes=["delivery:write"]))
):
    try:
        updates = payload.dict(exclude_unset=True)
        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update"
            )
            
        if "outlet_id" in updates:
            outlet = await outlet_manager.fetch(updates["outlet_id"])
            if not outlet.is_active:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Outlet is not active"
                )

        updated_dg = await delivery_guy_manager.update(delivery_guy_id, updates)
        
        user = await user_manager.fetch(updated_dg.user_id)
        outlet = await outlet_manager.fetch(updated_dg.outlet_id)
        user_resp = UserResponse.from_orm(user)
        outlet_resp = OutletResponse.from_orm(outlet)

        return DeliveryGuyResponse(
            uid=updated_dg.uid,
            user_id=updated_dg.user_id,
            outlet_id=updated_dg.outlet_id,
            is_active_for_delivery=updated_dg.is_active_for_delivery,
            payout_frequency=updated_dg.payout_frequency,
            is_deleted=updated_dg.is_deleted,
            created_at=updated_dg.created_at,
            user=user_resp,
            outlet=outlet_resp
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update delivery guy: {str(e)}"
        )

@router.delete("/{delivery_guy_id}", response_model=StatusResponse)
async def delete_delivery_guy(
    delivery_guy_id: str,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, allowed_scopes=["delivery:write"]))
):
    try:
        await delivery_guy_manager.update(delivery_guy_id, {"is_deleted": True, "is_active_for_delivery": False})
        return StatusResponse(status="ok", message="Delivery guy deleted successfully")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete delivery guy: {str(e)}"
        )

# --- Webhook Endpoints ---

@router.post("/webhooks/delivery-status", status_code=status.HTTP_200_OK)
async def update_delivery_status(payload: List[DeliveryStatusUpdatePayload], background_tasks: BackgroundTasks):
    """
    Webhook endpoint to receive delivery status updates for multiple orders.
    """
    results = []
    for item in payload:
        try:
            # Find the order
            try:
                order = await order_manager.fetch(item.order_id)
            except Exception:
                orders = await order_manager.fetch_all(filters={"order_number": item.order_id})
                if not orders.items:
                    results.append({"order_id": item.order_id, "status": "failed", "message": "Order not found"})
                    continue
                order = orders.items[0]

            order_uid = order.uid
            updates = {}
            new_status = order.order_status
            
            if item.status == "delivered":
                new_status = OrderStatus.DELIVERED
                updates["actual_delivery_date"] = datetime.utcnow()
                
                # Process reconciliation and inventory only if order status is changing to delivered
                if order.order_status != OrderStatus.DELIVERED:
                    # Auto-reconcile remaining balance (total_amount is the balance to be collected)
                    if order.total_amount > 0:
                        amount_to_collect = order.total_amount
                        transaction = OrderTransactionSchema(
                            order_id=order_uid,
                            payment_status=PaymentStatus.PAID,
                            payment_method=order.payment_method,
                            amount_paid=amount_to_collect,
                            notes=f"Auto-reconciled from bulk webhook. Remarks: {item.remarks}",
                            received_by=item.delivery_person_id or order.telecaller_id
                        )
                        await transaction_manager.create(transaction)
                        updates["has_auto_reconciled"] = True
                    
                    # Finalize inventory (deduct from quantity and reserved)
                    items = await order_item_manager.fetch_all(filters={"order_id": order_uid})
                    for order_item in items.items:
                        inv_records = await inventory_manager.fetch_all(
                            filters={"product_id": order_item.product_id, "outlet_id": order.assigned_outlet_id}
                        )
                        if inv_records.items:
                            inv = inv_records.items[0]
                            new_qty = max(0, inv.quantity - order_item.quantity)
                            new_reserved = max(0, inv.reserved_quantity - order_item.quantity)
                            await inventory_manager.update(inv.uid, {
                                "quantity": new_qty,
                                "reserved_quantity": new_reserved,
                                "last_updated": datetime.utcnow()
                            })
                    
                    # Calculate rider earning based on rate card
                    rate_cards = await rate_card_manager.fetch_all(filters={"outlet_id": order.assigned_outlet_id, "is_active": True})
                    if rate_cards.items:
                        rate_card = rate_cards.items[0]
                        updates["rider_earning"] = rate_card.pay_per_order
                    else:
                        # Fallback to 0 if no rate card found
                        updates["rider_earning"] = Decimal('0.00')

            elif item.status in ["postponed", "attempted"]:
                new_status = OrderStatus.POSTPONED if item.status == "postponed" else OrderStatus.ATTEMPTED
                if item.postpone_date:
                    updates["expected_delivery_date"] = item.postpone_date
                else:
                    updates["expected_delivery_date"] = datetime.utcnow().date() + timedelta(days=1)
                updates["priority_level"] = (order.priority_level or 0) + 10
                
            elif item.status == "cancelled":
                new_status = OrderStatus.CANCELLED
                updates["status_remarks"] = item.remarks
                items = await order_item_manager.fetch_all(filters={"order_id": order_uid})
                for order_item in items.items:
                    inv_records = await inventory_manager.fetch_all(
                        filters={"product_id": order_item.product_id, "outlet_id": order.assigned_outlet_id}
                    )
                    if inv_records.items:
                        inv = inv_records.items[0]
                        new_reserved = max(0, inv.reserved_quantity - order_item.quantity)
                        await inventory_manager.update(inv.uid, {
                            "reserved_quantity": new_reserved,
                            "last_updated": datetime.utcnow()
                        })
            else:
                results.append({"order_id": item.order_id, "status": "failed", "message": f"Unknown status: {item.status}"})
                continue

            updates["order_status"] = new_status
            await order_manager.update(order_uid, updates)

            tracking_record = DeliveryTrackingSchema(
                order_id=order_uid,
                outlet_id=order.assigned_outlet_id,
                telecaller_id=order.telecaller_id,
                delivery_person_id=item.delivery_person_id,
                status_changed_to=new_status,
                postpone_date=item.postpone_date,
                priority_level=updates.get("priority_level", 0),
                remarks=item.remarks,
                changed_by=item.delivery_person_id or order.telecaller_id
            )
            await tracking_manager.create(tracking_record)
            
            background_tasks.add_task(sync_order_to_crm, engine, order_uid, ActivityType.DELIVERY_STATUS)
            if updates.pop("has_auto_reconciled", False):
                background_tasks.add_task(sync_order_to_crm, engine, order_uid, ActivityType.PAYMENT_STATUS)

            results.append({"order_id": item.order_id, "status": "success", "new_status": new_status})

        except Exception as e:
            results.append({"order_id": item.order_id, "status": "failed", "message": str(e)})

    return {"status": "completed", "results": results}
