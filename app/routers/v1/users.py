from fastapi import APIRouter, HTTPException, Depends, status
from typing import List
import asyncio

from config import get_settings, get_engine
from managers import UserManager, LSQTelecallerMappingManager, UserSchema, LSQTelecallerMappingSchema
from services import CRMService
from models import (
    UserCreateRequest, UserUpdateRequest, UserPasswordChangeRequest,
    UserResponse, ListResponse, StatusResponse
)
from utils.auth import get_password_hash, verify_password, require_roles, get_current_user_id
from utils.constants import UserRole

settings = get_settings()
engine = get_engine(settings.name)
user_manager = UserManager(engine)
lsq_mapping_manager = LSQTelecallerMappingManager(engine)
crm_service = CRMService()

router = APIRouter(prefix="/users", tags=["User Management"])


# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

@router.post("/sync/telecallers", response_model=StatusResponse)
async def sync_lsq_telecallers(
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
):
    """
    Sync telecallers from LeadSquared to ERP.
    1. Fetches telecallers from LSQ.
    2. Creates new users in ERP if not present (password is email).
    3. Maps LSQ ID to ERP User ID; updates lsq_id and profile if already mapped.
    4. Deactivates ERP users who are no longer in LSQ payload.
    """
    try:
        # 1. Fetch telecallers from LSQ (Network call)
        lsq_users = await crm_service.get_lsq_telecallers()
        if not isinstance(lsq_users, list):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to fetch telecallers from LSQ"
            )

        # 2. Fetch all local data upfront (Bulk DB calls)
        # Fetching all mappings and all current telecallers to avoid lookups in the loop
        mappings_res = await lsq_mapping_manager.fetch_all(limit=0)
        users_res = await user_manager.fetch_all(filters={"role": UserRole.TELECALLER}, limit=0)
        
        # Build lookup maps for O(1) access
        email_to_mapping = {m.lsq_email.lower(): m for m in mappings_res.items if m.lsq_email}
        email_to_user = {u.email.lower(): u for u in users_res.items if u.email}
        
        lsq_emails_lower = {u.get("EmailAddress").lower() for u in lsq_users if u.get("EmailAddress")}

        created = 0
        updated = 0
        errors = []
        tasks = []

        # Helper to handle creation logic in parallel
        async def create_new_lsq_user(user_email, user_full_name, user_phone, user_lsq_id, is_active):
            try:
                new_user = UserSchema(
                    email=user_email,
                    full_name=user_full_name,
                    role=UserRole.TELECALLER,
                    phone=user_phone,
                    password_hash=get_password_hash(user_email),
                    is_active=is_active
                )
                created_user = await user_manager.create(new_user)
                new_mapping = LSQTelecallerMappingSchema(
                    lsq_id=user_lsq_id,
                    telecaller_id=created_user.uid,
                    lsq_email=user_email
                )
                await lsq_mapping_manager.create(new_mapping)
                return "created"
            except Exception as e:
                return f"error: {str(e)}"

        # 3. Identify and Queue Operations
        uids_processed = set()
        for user_data in lsq_users:
            email = user_data.get("EmailAddress")
            if not email:
                continue
            
            email_lower = email.lower()
            lsq_id = user_data.get("ID")
            first_name = user_data.get("FirstName", "")
            last_name = user_data.get("LastName", "")
            full_name = f"{first_name} {last_name}".strip() or "LSQ User"
            phone = user_data.get("Phone")
            
            # LeadSquared StatusCode: 0 is active, 1 is inactive
            lsq_active_status = user_data.get("StatusCode")
            is_active = (str(lsq_active_status) == "0")

            mapping = email_to_mapping.get(email_lower)
            erp_user = email_to_user.get(email_lower)

            if mapping:
                uids_processed.add(mapping.telecaller_id)
                # Update existing user and mapping if needed
                update_data = {
                    "is_active": is_active,
                    "role": UserRole.TELECALLER,
                    "full_name": full_name,
                    "phone": phone,
                    "email": email # Sync email in case it changed in LSQ
                }
                tasks.append(user_manager.update(mapping.telecaller_id, update_data))
                if mapping.lsq_id != lsq_id:
                    tasks.append(lsq_mapping_manager.update(mapping.uid, {"lsq_id": lsq_id}))
                updated += 1
            elif erp_user:
                uids_processed.add(erp_user.uid)
                # User exists but no mapping — update and create mapping
                update_data = {
                    "is_active": is_active,
                    "role": UserRole.TELECALLER,
                    "full_name": full_name,
                    "phone": phone,
                }
                tasks.append(user_manager.update(erp_user.uid, update_data))
                new_mapping = LSQTelecallerMappingSchema(
                    lsq_id=lsq_id,
                    telecaller_id=erp_user.uid,
                    lsq_email=email
                )
                tasks.append(lsq_mapping_manager.create(new_mapping))
                updated += 1
            else:
                # New user creation
                tasks.append(create_new_lsq_user(email, full_name, phone, lsq_id, is_active))
                created += 1

        # Execute all creates/updates in parallel
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    errors.append({"email": "Batch Update", "error": str(res)})
                elif isinstance(res, str) and res.startswith("error:"):
                    errors.append({"email": "Batch Create", "error": res[7:]})

        # 4. Bulk Deactivate users who are no longer in the LSQ payload
        # IMPORTANT: Use uids_processed to avoid deactivating users who were matched via mapping 
        # but had a different email in the users table.
        uids_to_deactivate = [
            u.uid for email, u in email_to_user.items() 
            if u.uid not in uids_processed and u.is_active
        ]
        
        deactivated = 0
        if uids_to_deactivate:
            try:
                await user_manager.update_all(
                    filters={"uid": uids_to_deactivate},
                    updates={"is_active": False},
                    limit=len(uids_to_deactivate)
                )
                deactivated = len(uids_to_deactivate)
            except Exception as e:
                errors.append({"email": "Bulk Deactivate", "error": str(e)})

        summary = f"Sync complete: {created} created, {updated} updated, {deactivated} deactivated."
        if errors:
            summary += f" {len(errors)} batch error(s)."

        return StatusResponse(status="success", message=summary)


    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sync failed: {str(e)}"
        )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    current_user_id: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER, UserRole.TELECALLER, UserRole.ACCOUNTANT))
):
    """Get specific user details"""
    try:
        # Users can view their own profile, admins and warehouse managers can view any user
        current_user = await user_manager.fetch(current_user_id)
        
        if current_user_id != user_id and current_user.role not in [UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: You can only view your own profile"
            )
        
        user = await user_manager.fetch(user_id)
        
        return UserResponse(
            uid=user.uid,
            email=user.email,
            full_name=user.full_name,
            phone=user.phone,
            role=user.role,
            outlet_id=user.outlet_id,
            is_active=user.is_active,
            created_at=user.created_at,
            last_login=user.last_login,
            updated_at=user.updated_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user: {str(e)}"
        )







@router.put("/{user_id}/password", response_model=StatusResponse)
async def change_user_password(
    user_id: str,
    payload: UserPasswordChangeRequest,
    current_user_id: str = Depends(get_current_user_id)
):
    """Change user password"""
    try:
        # Users can only change their own password unless admin
        if user_id != current_user_id:
            # Check if current user is admin
            current_user = await user_manager.fetch(current_user_id)
            if current_user.role not in [UserRole.SUPER_ADMIN, UserRole.ADMIN]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Can only change your own password"
                )
        
        # Verify old password
        user = await user_manager.fetch(user_id)
        if not verify_password(payload.old_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid old password"
            )
        
        # Update password
        new_password_hash = get_password_hash(payload.new_password)
        await user_manager.update(user_id, {"password_hash": new_password_hash})
        
        return StatusResponse(message="Password updated successfully")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to change password: {str(e)}"
        )


# GENERIC ROUTE LAST (after all specific routes)

@router.get("", response_model=ListResponse[UserResponse])
async def list_users(
    role: UserRole = None,
    outlet_id: str = None,
    is_active: bool = None,
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER, UserRole.OUTLET_MANAGER, UserRole.TELECALLER, UserRole.ACCOUNTANT))
):
    """
    List all users with optional filters
    Requires: super_admin, admin, accountant, or outlet_manager role
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


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN))
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
        user = UserSchema(
            email=payload.email,
            password_hash=get_password_hash(payload.password),  # Use bcrypt hashing
            full_name=payload.full_name,
            role=payload.role,
            phone=payload.phone,
            outlet_id=payload.outlet_id,
            is_active=True
        )
        
        created_user = await user_manager.create(user)
        
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



