from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings, get_engine
from managers import UserManager
from models import (
    LoginRequest, LoginResponse, RefreshTokenRequest, RefreshTokenResponse,
    ForgotPasswordRequest, ResetPasswordRequest, StatusResponse, UserResponse
)
from utils.auth import (
    verify_password, create_access_token, create_refresh_token,
    decode_token, get_current_user_id
)

settings = get_settings()
engine = get_engine(settings.name)
user_manager = UserManager(engine)

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
        
        # Verify password (truncate to 72 bytes for bcrypt compatibility)
        password_to_verify = payload.password[:72] if len(payload.password.encode('utf-8')) > 72 else payload.password
        if not verify_password(password_to_verify, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password"
            )
        
        # Update last login
        await user_manager.update(
            user.uid,
            {"last_login": "now()"}
        )
        
        # Create tokens
        token_data = {
            "sub": user.uid,
            "email": user.email,
            "role": user.role  # Remove .value since role is already a string in the database
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
        
        # Create new access token
        new_token_data = {
            "sub": token_data.get("sub"),
            "email": token_data.get("email"),
            "role": token_data.get("role")
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


@router.post("/forgot-password", response_model=StatusResponse)
async def forgot_password(payload: ForgotPasswordRequest):
    """
    Request password reset (sends email with reset token)
    TODO: Implement email sending
    """
    try:
        users = await user_manager.fetch_all(filters={"email": payload.email})
        
        if not users.items:
            # Don't reveal if email exists or not for security
            return StatusResponse(
                status="ok",
                message="If the email exists, a password reset link has been sent"
            )
        
        # TODO: Generate reset token and send email
        # For now, just return success
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
    TODO: Implement token validation
    """
    try:
        # TODO: Validate reset token and update password
        # For now, just return success
        return StatusResponse(
            status="ok",
            message="Password reset successfully"
        )
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Password reset failed: {str(e)}"
        )
