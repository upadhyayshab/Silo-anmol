from fastapi import APIRouter, HTTPException, Depends, status
from typing import List

import sqlalchemy as sa
from pydantic import BaseModel

from config import get_settings, get_engine
from managers import (UserManager, UserSchema, UserScopeAssignmentManager,
                      UserScopeAssignmentSchema)
from services.leadService import canon_state
from models import (
    UserCreateRequest, UserUpdateRequest, UserPasswordChangeRequest,
    UserResponse, ListResponse, StatusResponse
)
from utils.auth import (get_password_hash, verify_password,
                        require_permission, AuthContext,
                        enforce_agency_roster_fence, enforce_agency_update_fence,
                        get_auth_context)
from utils.permissions import Permission, ScopeLevel
from utils.constants import UserRole

settings = get_settings()
engine = get_engine(settings.name)
user_manager = UserManager(engine)
scope_assignment_manager = UserScopeAssignmentManager(engine)

# Multi-valued row-scope levels a user can be granted. GLOBAL roles need no row;
# OUTLET scope comes from the user's own `outlet_id` field (not an assignment row).
_ASSIGNABLE_LEVELS = {ScopeLevel.STATE.value, ScopeLevel.CLUSTER.value, ScopeLevel.AGENCY.value}


class ScopeAssignmentRequest(BaseModel):
    scope_level: str   # STATE | CLUSTER | AGENCY
    scope_value: str   # state name / cluster_id / agency_id


class ScopeAssignmentResponse(BaseModel):
    uid: str
    user_id: str
    scope_level: str
    scope_value: str

router = APIRouter(prefix="/users", tags=["User Management"])


# LSQ telecaller sync REMOVED (2026-07-10): its bulk-deactivate step twice took down
# the prod call floor once LSQ's user list went stale (LSQ is being decommissioned).
# Agents are created/managed in the ERP directly; scripts/reactivate_heartbeat_users.py
# repairs any past damage.

@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.USERS_READ)),
):
    """Get specific user details"""
    try:
        user = await user_manager.fetch(user_id)

        # Agency-roster fence: an AGENCY_ADMIN only sees users inside their own agency.
        if ctx.role == "AGENCY_ADMIN" and getattr(user, "agency_id", None) not in ctx.agency_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Out of agency scope")

        return UserResponse(
            uid=user.uid,
            email=user.email,
            full_name=user.full_name,
            phone=user.phone,
            role=user.role,
            outlet_id=user.outlet_id,
            agency_id=user.agency_id,
            state=user.state,
            is_active=user.is_active,
            assignment_quota=user.assignment_quota,
            created_at=user.created_at,
            last_login=user.last_login,
            last_active_at=user.last_active_at,
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
    ctx: AuthContext = Depends(get_auth_context)
):
    """Change user password.

    Self-service: a user changing their OWN password must prove the old one.
    Admin reset: a user with USERS_MANAGE resetting SOMEONE ELSE's password doesn't
    have the old one, so old_password is not required (and is ignored) on that path.
    """
    try:
        is_self = ctx.user_id == user_id
        is_admin = ctx.has(Permission.USERS_MANAGE)
        if not (is_self or is_admin):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Can only change your own password"
            )

        user = await user_manager.fetch(user_id)
        # Only the self-service path verifies the old password; an admin reset skips it.
        if is_self:
            if not payload.old_password or not verify_password(payload.old_password, user.password_hash):
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


# --------------------------------------------------------------------------
# Row-scope assignments (RBAC Step 3) — grant a user the clusters/states/agencies
# they cover. Multi-valued; consumed by login → JWT → apply_scope. Changes take
# effect on the user's next login / token refresh.
# --------------------------------------------------------------------------

@router.get("/{user_id}/scopes", response_model=List[ScopeAssignmentResponse])
async def list_user_scopes(
    user_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.USERS_MANAGE)),
):
    """List the row-scope grants for a user (which clusters/states/agencies they cover)."""
    target = await user_manager.fetch(user_id)
    enforce_agency_roster_fence(ctx, target.role, getattr(target, "agency_id", None))
    rows = await scope_assignment_manager.fetch_all(filters={"user_id": user_id}, limit=0)
    return [ScopeAssignmentResponse(uid=r.uid, user_id=r.user_id,
                                    scope_level=r.scope_level, scope_value=r.scope_value)
            for r in rows.items]


@router.post("/{user_id}/scopes", response_model=ScopeAssignmentResponse,
             status_code=status.HTTP_201_CREATED)
async def grant_user_scope(
    user_id: str,
    payload: ScopeAssignmentRequest,
    ctx: AuthContext = Depends(require_permission(Permission.USERS_MANAGE)),
):
    """Grant a row-scope (STATE / CLUSTER / AGENCY) to a user. Idempotent. A bad
    scope_value fails closed at query time (apply_scope expands to nothing → denied),
    so this only light-validates the level. Effective on the user's next login."""
    level = payload.scope_level.upper().strip()
    if level not in _ASSIGNABLE_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scope_level must be one of {sorted(_ASSIGNABLE_LEVELS)} "
                   "(GLOBAL roles need no grant; OUTLET scope is the user's outlet_id)")
    value = payload.scope_value.strip()
    if not value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="scope_value is required")

    target = await user_manager.fetch(user_id)
    enforce_agency_roster_fence(ctx, target.role, getattr(target, "agency_id", None))

    existing = await scope_assignment_manager.fetch_all(
        filters={"user_id": user_id, "scope_level": level, "scope_value": value}, limit=1)
    row = existing.items[0] if existing.items else await scope_assignment_manager.create(
        UserScopeAssignmentSchema(user_id=user_id, scope_level=level, scope_value=value))
    return ScopeAssignmentResponse(uid=row.uid, user_id=row.user_id,
                                   scope_level=row.scope_level, scope_value=row.scope_value)


@router.delete("/{user_id}/scopes/{assignment_id}", response_model=StatusResponse)
async def revoke_user_scope(
    user_id: str,
    assignment_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.USERS_MANAGE)),
):
    """Revoke a row-scope grant. Effective on the user's next login / token refresh."""
    target = await user_manager.fetch(user_id)
    enforce_agency_roster_fence(ctx, target.role, getattr(target, "agency_id", None))
    # Direct session delete — the base manager's delete(uid) does a fetch_one that
    # raises on this table; a plain select+delete is robust.
    async with scope_assignment_manager.session_factory() as s:
        row = (await s.execute(sa.select(UserScopeAssignmentSchema).where(
            UserScopeAssignmentSchema.uid == assignment_id))).scalars().first()
        if not row or row.user_id != user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scope assignment not found")
        await s.delete(row)
        await s.commit()
    return StatusResponse(status="ok", message="Scope revoked")


# GENERIC ROUTE LAST (after all specific routes)

@router.get("", response_model=ListResponse[UserResponse])
async def list_users(
    role: UserRole = None,
    outlet_id: str = None,
    is_active: bool = None,
    q: str = None,
    limit: int = 50,
    offset: int = 0,
    ctx: AuthContext = Depends(require_permission(Permission.USERS_READ)),
):
    """
    List all users with optional filters. `q` free-text searches name / phone / email / id.
    Requires: users:read permission
    """
    try:
        filters = {}
        if role:
            filters["role"] = role
        if outlet_id:
            filters["outlet_id"] = outlet_id
        if is_active is not None:
            filters["is_active"] = is_active

        if ctx.role == "AGENCY_ADMIN":
            filters["agency_id"] = ctx.agency_ids or ["__none__"]

        if q and q.strip():
            # Server-side search across the whole roster (count = total matches).
            items, count = await user_manager.search_users(
                q=q.strip(), filters=filters or None, limit=limit, offset=offset)
        else:
            page = await user_manager.fetch_all(
                limit=limit, offset=offset, filters=filters or None)
            items, count = page.items, len(page.items)
        
        user_responses = [
            UserResponse(
                uid=user.uid,
                email=user.email,
                full_name=user.full_name,
                role=user.role,
                phone=user.phone,
                outlet_id=user.outlet_id,
                agency_id=user.agency_id,
                state=user.state,
                is_active=user.is_active,
                assignment_quota=user.assignment_quota,
                last_login=user.last_login,
                last_active_at=user.last_active_at,
                created_at=user.created_at,
                updated_at=user.updated_at
            )
            for user in items
        ]

        return ListResponse(items=user_responses, count=count)
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch users: {str(e)}"
        )


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateRequest,
    ctx: AuthContext = Depends(require_permission(Permission.USERS_MANAGE)),
):
    """
    Create new user
    Requires: USERS_MANAGE permission
    """
    try:
        enforce_agency_roster_fence(ctx, payload.role, payload.agency_id)
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
            agency_id=payload.agency_id,
            state=canon_state(payload.state),
            assignment_quota=payload.assignment_quota,
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
            agency_id=created_user.agency_id,
            state=created_user.state,
            is_active=created_user.is_active,
            assignment_quota=created_user.assignment_quota,
            last_login=created_user.last_login,
            last_active_at=created_user.last_active_at,
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
    ctx: AuthContext = Depends(require_permission(Permission.USERS_MANAGE)),
):
    """
    Update user details
    Requires: USERS_MANAGE permission
    """
    try:
        target = await user_manager.fetch(user_id)
        updates = payload.dict(exclude_unset=True)
        if "state" in updates:
            updates["state"] = canon_state(updates["state"])
        enforce_agency_update_fence(ctx, target.role, getattr(target, "agency_id", None), updates)

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
            agency_id=updated_user.agency_id,
            state=updated_user.state,
            is_active=updated_user.is_active,
            assignment_quota=updated_user.assignment_quota,
            last_login=updated_user.last_login,
            last_active_at=updated_user.last_active_at,
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
    ctx: AuthContext = Depends(require_permission(Permission.USERS_MANAGE)),
):
    """
    Deactivate user (soft delete)
    Requires: USERS_MANAGE permission
    """
    try:
        target = await user_manager.fetch(user_id)
        enforce_agency_roster_fence(ctx, target.role, getattr(target, "agency_id", None))
        await user_manager.update(user_id, {"is_active": False})
        # Release the deactivated user's leads so the 5-min sweep redistributes them
        # (else they sit stranded on a dead owner).
        from services.leadService import release_leads_of_users
        await release_leads_of_users(engine, [user_id])
        return StatusResponse(status="ok", message="User deactivated successfully")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to deactivate user: {str(e)}"
        )



