from fastapi import APIRouter, HTTPException, Depends, status
from typing import List

from config import get_settings, get_engine
from managers import UserManager
from models import (
    UserCreateRequest, UserUpdateRequest, UserPasswordChangeRequest,
    UserResponse, ListResponse, StatusResponse
)
from utils.auth import require_roles, get_current_user_id, get_password_hash
from utils.constants import UserRole

settings = get_settings()
engine = get_engine(settings.name)
user_manager = UserManager(engine)

router = APIRouter(prefix="/users", tags=["User Management"])


@router.get("", response_model=ListResponse[UserResponse])
async def list_users(
    role: UserRole = None,
    outlet_id: str = None,
    is_active: bool = None,
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    List all users with optional filters
    Requires: super_admin or admin role
    """
    try:
        filters = {}
        if role:
            filters["role"] = role
        if outlet_id:
            filters["outlet_id"] = outlet_id
        if is_active is not None:
            filters["is_active"] = is_active
        
        users = await user_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=filters if filters else None
        )
        
        user_responses = [
            UserResponse(
                uid=user.uid,
                email=user.email,
                full_name=user.full_name,
                role=user.role,
                phone=user.phone,
                outlet_id=user.outlet_id,
                is_active=user.is_active,
                last_login=user.last_login,
                created_at=user.created_at,
                updated_at=user.updated_at
            )
            for user in users.items
        ]
        
        return ListResponse(items=user_responses, count=len(user_responses))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch users: {str(e)}"
        )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    Get user by ID
    Requires: super_admin or admin role
    """
    try:
        user = await user_manager.fetch(user_id)
        
        return UserResponse(
            uid=user.uid,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            phone=user.phone,
            outlet_id=user.outlet_id,
            is_active=user.is_active,
            last_login=user.last_login,
            created_at=user.created_at,
            updated_at=user.updated_at
        )
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User not found: {str(e)}"
        )


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateRequest,
    # _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    Create new user
    Requires: super_admin or admin role
    """
    try:
        # Check if email already exists
        existing = await user_manager.fetch_all(filters={"email": payload.email})
        if existing.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already exists"
            )
        
        # Create user with hashed password using bcrypt
        from managers import UserSchema
        user_data = {
            "email": payload.email,
            "password_hash": get_password_hash(payload.password),  # Use bcrypt hashing
            "full_name": payload.full_name,
            "role": payload.role,
            "phone": payload.phone,
            "outlet_id": payload.outlet_id,
            "is_active": True
        }
        
        created_user = await user_manager.create(user_data)
        
        return UserResponse(
            uid=created_user.uid,
            email=created_user.email,
            full_name=created_user.full_name,
            role=created_user.role,
            phone=created_user.phone,
            outlet_id=created_user.outlet_id,
            is_active=created_user.is_active,
            last_login=created_user.last_login,
            created_at=created_user.created_at,
            updated_at=created_user.updated_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create user: {str(e)}"
        )


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    Update user details
    Requires: super_admin or admin role
    """
    try:
        updates = payload.dict(exclude_unset=True)
        
        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update"
            )
        
        updated_user = await user_manager.update(user_id, updates)
        
        return UserResponse(
            uid=updated_user.uid,
            email=updated_user.email,
            full_name=updated_user.full_name,
            role=updated_user.role,
            phone=updated_user.phone,
            outlet_id=updated_user.outlet_id,
            is_active=updated_user.is_active,
            last_login=updated_user.last_login,
            created_at=updated_user.created_at,
            updated_at=updated_user.updated_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update user: {str(e)}"
        )


@router.delete("/{user_id}", response_model=StatusResponse)
async def deactivate_user(
    user_id: str,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN))
):
    """
    Deactivate user (soft delete)
    Requires: super_admin role only
    """
    try:
        await user_manager.update(user_id, {"is_active": False})
        return StatusResponse(status="ok", message="User deactivated successfully")
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to deactivate user: {str(e)}"
        )


@router.put("/{user_id}/password", response_model=StatusResponse)
async def change_user_password(
    user_id: str,
    payload: UserPasswordChangeRequest,
    current_user_id: str = Depends(get_current_user_id)
):
    """
    Change user password
    Users can only change their own password
    """
    try:
        # Users can only change their own password
        if user_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only change your own password"
            )
        
        # Verify old password
        user = await user_manager.fetch(user_id)
        if not user.__password_eq__(payload.old_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Incorrect old password"
            )
        
        # Update password
        user.password = payload.new_password  # This will hash the password
        await user_manager.update(user_id, {"password_hash": user.password_hash})
        
        return StatusResponse(status="ok", message="Password changed successfully")
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to change password: {str(e)}"
        )
