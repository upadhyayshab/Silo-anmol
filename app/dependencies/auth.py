"""
JWT authentication FastAPI dependency.

Validates the `x-access-jwt` header using the application JWT_SECRET (HS256).
Expected token payload: {"upid": "users_<uuid>", "exp": <unix_timestamp>}

Returns the decoded payload dict on success.
Raises HTTP 401 on missing, expired, or invalid tokens.
"""
from typing import Optional

import jwt
from fastapi import Header, HTTPException

from config import get_settings


async def require_jwt(x_access_jwt: Optional[str] = Header(None, alias="x-access-jwt")) -> dict:
    """FastAPI dependency that validates the x-access-jwt header.

    Usage:
        @router.post("/recommend")
        async def endpoint(payload: dict = Depends(require_jwt)):
            upid = payload["upid"]
    """
    # Optional in the signature so a missing header is a 401 (log in again),
    # not FastAPI's 422 validation error.
    if not x_access_jwt:
        raise HTTPException(status_code=401, detail="Missing x-access-jwt header")
    settings = get_settings()
    if not settings.jwt_secret:
        raise HTTPException(status_code=500, detail="JWT secret not configured on server")

    try:
        decoded = jwt.decode(
            x_access_jwt,
            settings.jwt_secret,
            algorithms=["HS256"],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    upid = decoded.get("upid")
    if not upid:
        raise HTTPException(status_code=401, detail="Token payload missing 'upid' field")

    return decoded


__all__ = ["require_jwt"]
