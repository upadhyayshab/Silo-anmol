from fastapi import APIRouter, HTTPException, Depends, status, Body
from typing import List, Optional

from config import get_settings, get_engine
from managers import DeliveryGuyManager, UserManager, OutletManager, DeliveryGuySchema, UserSchema
from models import (
    DeliveryGuyCreateRequest, DeliveryGuyUpdateRequest, DeliveryGuyResponse,
    ListResponse, StatusResponse, UserResponse, OutletResponse
)
from utils.auth import require_roles, get_current_user_id, get_password_hash
from utils.constants import UserRole

settings = get_settings()
engine = get_engine(settings.name)
delivery_guy_manager = DeliveryGuyManager(engine)
user_manager = UserManager(engine)
outlet_manager = OutletManager(engine)

router = APIRouter(prefix="/delivery-guys", tags=["Delivery Guy Management"])

@router.get("/{delivery_guy_id}", response_model=DeliveryGuyResponse)
async def get_delivery_guy(
    delivery_guy_id: str,
    # _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER))
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
    outlet_id: Optional[str] = None,
    is_active_for_delivery: Optional[bool] = None,
    is_deleted: Optional[bool] = False,
    limit: int = 50,
    offset: int = 0,
    # _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER))
):
    try:
        filters = {}
        if outlet_id:
            filters["outlet_id"] = outlet_id
        if is_active_for_delivery is not None:
            filters["is_active_for_delivery"] = is_active_for_delivery
        if is_deleted is not None:
            filters["is_deleted"] = is_deleted

        delivery_guys = await delivery_guy_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=filters if filters else None
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
    # _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    try:
        # 1. Check if user already exists
        existing_user = await user_manager.fetch_all(filters={"email": payload.email})
        if existing_user.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User with this email already exists"
            )
            
        # 2. Check if outlet exists
        outlet = await outlet_manager.fetch(payload.outlet_id)
        if not outlet.is_active:
             raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Outlet is not active"
            )

        # 3. Create User first
        user = UserSchema(
            email=payload.email,
            password_hash=get_password_hash(payload.phone),
            full_name=payload.full_name,
            role=UserRole.DELIVERY_GUY,
            phone=payload.phone,
            outlet_id=payload.outlet_id,
            is_active=True
        )
        created_user = await user_manager.create(user)

        # 4. Create Delivery Guy profile
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
    # _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
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
    # _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    try:
        await delivery_guy_manager.update(delivery_guy_id, {"is_deleted": True, "is_active_for_delivery": False})
        return StatusResponse(status="ok", message="Delivery guy deleted successfully")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete delivery guy: {str(e)}"
        )
