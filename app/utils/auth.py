"""Authentication utilities for JWT and password management"""
from datetime import datetime, timedelta, UTC
from typing import Optional
import jwt
import bcrypt
from fastapi import HTTPException, Depends, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import get_settings
from utils.constants import UserRole

# Get settings instance
settings = get_settings()

# JWT Configuration from settings
SECRET_KEY = settings.jwt_secret_key
ALGORITHM = settings.jwt_algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.jwt_access_token_expire_minutes
REFRESH_TOKEN_EXPIRE_DAYS = settings.jwt_refresh_token_expire_days

security = HTTPBearer(auto_error=False)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash using bcrypt directly"""
    try:
        # Handle both string and bytes formats
        if isinstance(hashed_password, str):
            hashed_bytes = hashed_password.encode('utf-8')
        else:
            hashed_bytes = hashed_password
            
        if isinstance(plain_password, str):
            plain_bytes = plain_password.encode('utf-8')
        else:
            plain_bytes = plain_password
            
        return bcrypt.checkpw(plain_bytes, hashed_bytes)
    except Exception as e:
        print(f"Password verification error: {e}")
        return False


def get_password_hash(password: str) -> str:
    """Hash a password using bcrypt directly"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire, "type": "access"})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(data: dict) -> str:
    """Create JWT refresh token"""
    to_encode = data.copy()
    expire = datetime.now(UTC) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> dict:
    """Decode and verify JWT token"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user_id(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> str:
    """Extract user ID from JWT token or allow microservice API Key"""
    if not credentials:
        if getattr(request.state, "scopes", None) is not None:
            return "microservice"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    token = credentials.credentials
    payload = decode_token(token)
    
    user_id: str = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )
    
    return user_id


# ============================================================================
# PERMISSION + SCOPE ENFORCEMENT (RBAC blueprint). require_roles was retired
# 2026-06-29 once every router moved to require_permission. See utils/permissions.py and RBAC_ACCESS_BLUEPRINT.md.
# ============================================================================
from dataclasses import dataclass, field
from utils.permissions import (
    ScopeLevel, WILDCARD, role_perms, role_scope_level, has_permission,
    masked_columns_for, _perm_value,
)


@dataclass
class AuthContext:
    """Resolved identity passed downstream for scope filtering + field masking."""
    user_id: str
    role: Optional[str] = None
    is_microservice: bool = False
    scope_level: str = ScopeLevel.OUTLET.value
    outlet_id: Optional[str] = None
    states: list = field(default_factory=list)        # STATE scope (multi-valued)
    cluster_ids: list = field(default_factory=list)   # CLUSTER scope (multi-valued)
    agency_ids: list = field(default_factory=list)   # AGENCY scope (admin's agencies)
    perms: set = field(default_factory=set)

    @property
    def scope_value(self):
        if self.scope_level == ScopeLevel.OUTLET.value:
            return self.outlet_id
        if self.scope_level == ScopeLevel.STATE.value:
            return self.states
        if self.scope_level == ScopeLevel.CLUSTER.value:
            return self.cluster_ids
        if self.scope_level == ScopeLevel.AGENCY.value:
            return self.agency_ids
        return None

    def has(self, perm) -> bool:
        if WILDCARD in self.perms:
            return True
        return _perm_value(perm) in self.perms


def _build_context(request: Request, credentials: Optional[HTTPAuthorizationCredentials]) -> AuthContext:
    """Authenticate (API key or JWT) and resolve perms + scope. No permission gate."""
    # Microservice path: SDKMiddleware put verified scopes on request.state.
    req_scopes = getattr(request.state, "scopes", None)
    if not credentials and req_scopes is not None:
        return AuthContext(
            user_id="microservice", is_microservice=True,
            scope_level=ScopeLevel.GLOBAL.value, perms=set(req_scopes),
        )
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    role = payload.get("role")
    if user_id is None or role is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )
    return AuthContext(
        user_id=user_id,
        role=role,
        scope_level=role_scope_level(role).value,
        outlet_id=payload.get("outlet_id"),
        states=payload.get("states") or ([payload["state"]] if payload.get("state") else []),
        cluster_ids=payload.get("cluster_ids") or [],
        agency_ids=payload.get("agency_ids") or [],
        perms=role_perms(role),
    )


async def get_auth_context(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> AuthContext:
    """Dependency: authenticated context with resolved perms/scope, no perm gate."""
    return _build_context(request, credentials)


def require_permission(perm, *, allow_scopes: Optional[list] = None):
    """Dependency factory: gate an endpoint on a single Permission.

    Mirrors require_roles' dual path — an API key whose scopes intersect the
    required permission (or `allow_scopes`) passes as a microservice; otherwise
    the JWT role must hold the permission. Returns an AuthContext.
    """
    async def checker(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    ) -> AuthContext:
        req_scopes = getattr(request.state, "scopes", None)
        if req_scopes is not None:
            allowed = set(allow_scopes or []) | {_perm_value(perm)}
            if allowed.intersection(req_scopes):
                return AuthContext(
                    user_id="microservice", is_microservice=True,
                    scope_level=ScopeLevel.GLOBAL.value, perms=set(req_scopes),
                )
        ctx = _build_context(request, credentials)
        if not ctx.has(perm):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {_perm_value(perm)}",
            )
        return ctx
    return checker


_outlet_mgr = None


def _get_outlet_mgr():
    # Lazy: keeps utils.auth importable without pulling the managers tree (tests).
    global _outlet_mgr
    if _outlet_mgr is None:
        from config import get_engine
        from managers import OutletManager
        _outlet_mgr = OutletManager(get_engine(settings.name))
    return _outlet_mgr


async def _scoped_outlet_ids(ctx: AuthContext) -> list:
    """Expand a STATE/CLUSTER user's assignments to the outlet ids they cover."""
    mgr = _get_outlet_mgr()
    if ctx.scope_level == ScopeLevel.CLUSTER.value and ctx.cluster_ids:
        res = await mgr.fetch_all(filters={"cluster_id": ctx.cluster_ids}, limit=0)
        return [o.uid for o in res.items]
    if ctx.scope_level == ScopeLevel.STATE.value and ctx.states:
        res = await mgr.fetch_all(filters={"state": ctx.states}, limit=0)
        return [o.uid for o in res.items]
    return []


async def outlet_ids_for_state(state: Optional[str]) -> list:
    """Resolve a state name to the uids of outlets located in it (case-insensitive).

    Lets GLOBAL callers (super admin) voluntarily narrow order/report views to one
    state. Returns [] for a blank state (caller applies no filter). For a *non-blank*
    state that matches no outlet, callers must fall back to the "__none__" sentinel so
    the query matches nothing instead of returning every row — mirror `apply_scope`:

        if state:
            ids = await outlet_ids_for_state(state)
            filters[outlet_column] = ids or ["__none__"]
    """
    if not state or not str(state).strip():
        return []
    # Fold in states that operationally serve the requested one: Telangana leads are
    # worked by the AP team (STATE_ALIASES), but their warehouse outlets are still tagged
    # 'Telangana' — so an explicit "Andhra Pradesh" filter must also match those, else it
    # under-counts (they'd only show under "All Regions"). Reverse the alias map so the
    # served-elsewhere state's outlets are included with the serving state.
    from services.leadService import STATE_ALIASES  # lazy: avoid utils<->services import cycle
    canon = str(state).strip().lower()
    names = {canon} | {src for src, dst in STATE_ALIASES.items() if dst == canon}
    mgr = _get_outlet_mgr()
    ids = []
    for name in names:  # ≤2 states; $ieq is case-insensitive so lowercase matches stored casing
        res = await mgr.fetch_all(filters={"state": {"$ieq": name}}, limit=0)
        ids.extend(o.uid for o in res.items)
    return ids


_user_mgr = None


def _get_user_mgr():
    global _user_mgr
    if _user_mgr is None:
        from config import get_engine
        from managers import UserManager
        _user_mgr = UserManager(get_engine(settings.name))
    return _user_mgr


async def _scoped_telecaller_ids(ctx: AuthContext) -> list:
    """Expand an agency admin's agencies to the telecaller user-ids they cover."""
    if not ctx.agency_ids:
        return []
    res = await _get_user_mgr().fetch_all(filters={"agency_id": ctx.agency_ids}, limit=0)
    return [u.uid for u in res.items]


async def apply_scope(filters: Optional[dict], ctx: AuthContext, outlet_column: str = "outlet_id") -> dict:
    """Narrow filters to the caller's row scope and return them.

    OVERRIDES `outlet_column` for scoped roles so query params can't escape scope;
    no-op for GLOBAL / microservice. STATE/CLUSTER expand to the covered outlet ids
    (a user may manage several). Deny-by-default: a scoped user with no assignment
    matches nothing (the "__none__" sentinel never equals a real uuid uid).
    Endpoints whose outlet FK isn't `outlet_id` pass `outlet_column=...`.
    """
    out = dict(filters or {})
    if ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value:
        return out
    if ctx.scope_level == ScopeLevel.AGENCY.value:
        ids = await _scoped_telecaller_ids(ctx)
        out["telecaller_id"] = ids or ["__none__"]
        return out
    if ctx.scope_level == ScopeLevel.OUTLET.value:
        out[outlet_column] = ctx.outlet_id or "__none__"
        return out
    ids = await _scoped_outlet_ids(ctx)
    out[outlet_column] = ids or ["__none__"]
    return out


def mask_fields(obj, columns: list):
    """Null the given attributes/keys on a pydantic model, ORM row, or dict (in place)."""
    if not columns:
        return obj
    if isinstance(obj, dict):
        for c in columns:
            if c in obj:
                obj[c] = None
    else:
        for c in columns:
            if hasattr(obj, c):
                try:
                    setattr(obj, c, None)
                except Exception:
                    pass
    return obj


def apply_field_mask(resource: str, ctx: AuthContext, data):
    """Strip sensitive columns (cost/margin, tax ids) the caller may not see.

    Accepts a single object or a list; mutates and returns it. Microservices and
    wildcard holders are never masked.
    """
    if ctx.is_microservice:
        return data
    columns = masked_columns_for(resource, ctx.role)
    if not columns:
        return data
    items = data if isinstance(data, (list, tuple)) else [data]
    for item in items:
        mask_fields(item, columns)
    return data


def enforce_agency_roster_fence(ctx: "AuthContext", target_role, target_agency_id) -> None:
    """An AGENCY_ADMIN may only create/modify AGENCY_TELECALLERs inside their own
    agency. No-op for everyone else. Raises 403 on violation."""
    if ctx.role != "AGENCY_ADMIN":
        return
    role_val = target_role.value if hasattr(target_role, "value") else str(target_role)
    if role_val != "AGENCY_TELECALLER":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Agency admins may only manage AGENCY_TELECALLER accounts")
    if target_agency_id not in ctx.agency_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Agency admins may only manage their own agency")


def enforce_agency_update_fence(ctx: "AuthContext", target_role, target_agency_id, updates: dict) -> None:
    """Fence for modifying an existing user. First the create-time fence (target
    must be an own-agency AGENCY_TELECALLER), then forbid an AGENCY_ADMIN from
    re-targeting the member's agency or outlet."""
    enforce_agency_roster_fence(ctx, target_role, target_agency_id)
    if ctx.role != "AGENCY_ADMIN":
        return
    if "agency_id" in updates and updates["agency_id"] not in ctx.agency_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Agency admins may not move a member to another agency")
    if "outlet_id" in updates:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Agency admins may not change a member's outlet")


__all__ = [
    "verify_password",
    "get_password_hash",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "get_current_user_id",
    # RBAC blueprint
    "AuthContext",
    "get_auth_context",
    "require_permission",
    "apply_scope",
    "mask_fields",
    "apply_field_mask",
    "enforce_agency_roster_fence",
    "enforce_agency_update_fence",
]
