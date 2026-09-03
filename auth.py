"""
Simple single-password auth for the dashboard.

There are no user accounts here - just one shared DASHBOARD_PASSWORD (set in .env).
A successful login issues a signed JWT valid for TOKEN_TTL_SECONDS. Every protected
route re-validates that token via require_auth, and - as long as it's still valid -
issues a fresh one with a renewed expiry in the response headers ("sliding expiration"),
so a token only actually goes stale if the dashboard sits completely unused for the
full window. There's no server-side session storage: anyone who changes JWT_SECRET
instantly invalidates every token in existence, which doubles as a manual kill switch.
"""

import os
import time
import secrets
import logging

import jwt
from fastapi import Header, HTTPException, Response

logger = logging.getLogger(__name__)

JWT_SECRET = os.getenv("JWT_SECRET")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

if not JWT_SECRET:
    logger.warning("JWT_SECRET is not set in .env - login will fail until it's configured.")
if not DASHBOARD_PASSWORD:
    logger.warning("DASHBOARD_PASSWORD is not set in .env - login will fail until it's configured.")


def create_token() -> str:
    payload = {"exp": time.time() + TOKEN_TTL_SECONDS}
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def verify_password(password: str) -> bool:
    if not DASHBOARD_PASSWORD:
        return False
    # Constant-time comparison so response timing can't be used to guess the password.
    return secrets.compare_digest(password, DASHBOARD_PASSWORD)


def require_auth(response: Response, authorization: str = Header(None)):
    """FastAPI dependency: validates the Bearer token on the request and, if still
    valid, attaches a freshly-renewed token to the response (sliding expiration)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = authorization.split(" ", 1)[1]
    try:
        jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired, please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session")

    response.headers["X-New-Token"] = create_token()
