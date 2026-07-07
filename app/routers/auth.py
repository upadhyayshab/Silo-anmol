from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings, get_engine
from managers import UserManager, UserScopeAssignmentManager
from models import (
    LoginRequest, LoginResponse, RefreshTokenRequest, RefreshTokenResponse,
    ForgotPasswordRequest, ResetPasswordRequest, StatusResponse, UserResponse
)
from utils.auth import (
    verify_password, create_access_token, create_refresh_token,
    decode_token, get_current_user_id, get_auth_context, AuthContext
)
from utils.permissions import masked_columns_for

settings = get_settings()
engine = get_engine(settings.name)
user_manager = UserManager(engine)
scope_assignment_manager = UserScopeAssignmentManager(engine)

router = APIRouter(tags=["Authentication"])


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest):
    """
    User login with email and password
    Returns access token and refresh token
    """
    try:
        # Fetch user by email
        users = await user_manager.fetch_all(filters={"email": payload.email})
        
        if not users.items:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password"
            )
        
        user = users.items[0]
        
        # Check if user is active
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User account is deactivated"
            )
        
        # Verify password
        if not verify_password(payload.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password"
            )
        
        # Update last login
        from datetime import datetime
        await user_manager.update(
            user.uid,
            {"last_login": datetime.now()}
        )
        
        # Resolve multi-valued scope (a user may manage several clusters/states).
        assignments = await scope_assignment_manager.fetch_all(filters={"user_id": user.uid}, limit=0)
        cluster_ids = [a.scope_value for a in assignments.items if a.scope_level == "CLUSTER"]
        states = [a.scope_value for a in assignments.items if a.scope_level == "STATE"]
        agency_ids = [a.scope_value for a in assignments.items if a.scope_level == "AGENCY"]
        # No explicit AGENCY grants → default to the user's own agency (the common
        # single-agency case; the assignments table is for multi-agency admins).
        if not agency_ids and getattr(user, "agency_id", None):
            agency_ids = [user.agency_id]

        # Scope claims ride along so require_permission/apply_scope filter rows
        # without a per-request DB hit (outlet expansion still queries on use).
        token_data = {
            "sub": user.uid,
            "email": user.email,
            "role": user.role,  # already a string in the database
            "outlet_id": user.outlet_id,
            "state": getattr(user, "state", None),
            "cluster_ids": cluster_ids,
            "states": states,
            "agency_ids": agency_ids,
        }
        access_token = create_access_token(token_data)
        refresh_token = create_refresh_token(token_data)
        
        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            user_id=user.uid,
            email=user.email,
            full_name=user.full_name,
            role=user.role  # Remove .value since role is already a string
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Login failed: {str(e)}"
        )


@router.post("/refresh-token", response_model=RefreshTokenResponse)
async def refresh_token(payload: RefreshTokenRequest):
    """
    Refresh access token using refresh token
    """
    try:
        # Decode refresh token
        token_data = decode_token(payload.refresh_token)
        
        # Verify it's a refresh token
        if token_data.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type"
            )
        
        # Create new access token (preserve scope claims from the refresh token)
        new_token_data = {
            "sub": token_data.get("sub"),
            "email": token_data.get("email"),
            "role": token_data.get("role"),
            "outlet_id": token_data.get("outlet_id"),
            "state": token_data.get("state"),
            "cluster_ids": token_data.get("cluster_ids") or [],
            "states": token_data.get("states") or [],
            "agency_ids": token_data.get("agency_ids") or [],
        }
        access_token = create_access_token(new_token_data)
        
        return RefreshTokenResponse(access_token=access_token)
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Token refresh failed: {str(e)}"
        )


@router.post("/logout", response_model=StatusResponse)
async def logout(user_id: str = Depends(get_current_user_id)):
    """
    Logout user (client should discard tokens)
    """
    # In a production system, you might want to blacklist the token
    # For now, we just return success and let client handle token removal
    return StatusResponse(status="ok", message="Logged out successfully")


@router.get("/me", response_model=UserResponse)
async def get_current_user(user_id: str = Depends(get_current_user_id)):
    """
    Get current authenticated user profile
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


@router.get("/access")
async def get_access(ctx: AuthContext = Depends(get_auth_context)):
    """
    Report the caller's resolved RBAC access: role, scope, granted permissions,
    and which sensitive columns are masked for them. Lets the frontend gate menus
    and fields from one source instead of hard-coding role lists.
    """
    return {
        "user_id": ctx.user_id,
        "role": ctx.role,
        "is_microservice": ctx.is_microservice,
        "scope_level": ctx.scope_level,
        "scope_value": ctx.scope_value,
        "permissions": sorted(ctx.perms),
        "masked_fields": {
            "products": masked_columns_for("products", ctx.role),
            "outlets": masked_columns_for("outlets", ctx.role),
        },
    }


@router.post("/forgot-password", response_model=StatusResponse)
async def forgot_password(payload: ForgotPasswordRequest):
    """
    Request password reset (generates reset token)
    """
    try:
        users = await user_manager.fetch_all(filters={"email": payload.email})
        
        if not users.items:
            # Don't reveal if email exists or not for security
            return StatusResponse(
                status="ok",
                message="If the email exists, a password reset link has been sent"
            )
        
        user = users.items[0]
        
        # Generate reset token (valid for 1 hour)
        import secrets
        from datetime import datetime, timedelta, timezone
        
        reset_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        
        # Update user with reset token
        await user_manager.update(user.uid, {
            "password_reset_token": reset_token,
            "password_reset_expires_at": expires_at
        })
        
        # TODO: Send email with reset token
        # For now, log the token (remove this in production)
        print(f"Password reset token for {user.email}: {reset_token}")
        
        return StatusResponse(
            status="ok",
            message="If the email exists, a password reset link has been sent"
        )
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Password reset request failed: {str(e)}"
        )


@router.post("/reset-password", response_model=StatusResponse)
async def reset_password(payload: ResetPasswordRequest):
    """
    Reset password using reset token
    """
    try:
        from datetime import datetime, timezone
        from utils.auth import hash_password
        
        # Find user by reset token
        users = await user_manager.fetch_all(filters={"password_reset_token": payload.token})
        
        if not users.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset token"
            )
        
        user = users.items[0]
        
        # Check if token is expired
        if not user.password_reset_expires_at or user.password_reset_expires_at < datetime.now(timezone.utc):
            # Clear expired token
            await user_manager.update(user.uid, {
                "password_reset_token": None,
                "password_reset_expires_at": None
            })
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset token"
            )
        
        # Validate new password
        if len(payload.new_password) < 8:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password must be at least 8 characters long"
            )
        
        # Hash new password and clear reset token
        hashed_password = hash_password(payload.new_password)
        
        await user_manager.update(user.uid, {
            "password_hash": hashed_password,
            "password_reset_token": None,
            "password_reset_expires_at": None
        })
        
        return StatusResponse(
            status="ok",
            message="Password reset successfully"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Password reset failed: {str(e)}"
        )
