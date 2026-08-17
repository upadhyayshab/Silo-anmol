"""
JWT authentication FastAPI dependency.

Validates the `x-access-jwt` header using the application JWT_SECRET (HS256).
Expected token payload: {"upid": "users_<uuid>", "exp": <unix_timestamp>}

Returns the decoded payload dict on success.
Raises HTTP 401 on missing, expired, or invalid tokens.
"""
import jwt
from fastapi import Header, HTTPException

from config import get_settings


async def require_jwt(x_access_jwt: str = Header(..., alias="x-access-jwt")) -> dict:
    """FastAPI dependency that validates the x-access-jwt header.

    Usage:
        @router.post("/recommend")
        async def endpoint(payload: dict = Depends(require_jwt)):
            upid = payload["upid"]
    """
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
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")

    upid = decoded.get("upid")
    if not upid:
        raise HTTPException(status_code=401, detail="Token payload missing 'upid' field")

    return decoded


__all__ = ["require_jwt"]
