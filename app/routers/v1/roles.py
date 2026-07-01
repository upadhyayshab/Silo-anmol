"""Superadmin roles & permissions admin API.

DB is authoritative for role/permission *assignments* (`roles` + `role_permissions`,
see `services/roleStore.py`); code (`utils.permissions.ALL_PERMISSIONS`) is authoritative
for the permission *vocabulary*. Every write here refreshes the live resolver cache
(`roleStore.refresh_role_cache`) so edits apply without a redeploy, and writes one
`ActivityLogSchema` audit row. Gated on `Permission.ROLES_MANAGE`, which only
`SUPER_ADMIN` holds (via WILDCARD) — this is the superadmin-only gate.
"""
import logging

import sqlalchemy as db
from fastapi import APIRouter, Depends, HTTPException, Request, status

from config import get_settings, get_engine
from managers import (
    RoleManager, RoleSchema, RolePermissionManager, RolePermissionSchema,
    ActivityLogManager, ActivityLogSchema,
)
from models import RoleCreateRequest, RolePermsUpdateRequest, RoleItemResponse, RolesCatalogResponse
from services import roleStore
from utils.auth import require_permission, AuthContext
from utils.permissions import (
    Permission, ALL_PERMISSIONS, validate_role_perms, validate_scope_level,
    validate_new_role_name, diff_perms,
)

logger = logging.getLogger(__name__)

settings = get_settings()
engine = get_engine(settings.name)
role_manager = RoleManager(engine)
role_permission_manager = RolePermissionManager(engine)
activity_manager = ActivityLogManager(engine)

router = APIRouter(prefix="/roles", tags=["Roles"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else None


@router.get("", response_model=RolesCatalogResponse)
async def list_roles(
    ctx: AuthContext = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    """Full roles catalog: every non-deleted role + its perms, plus the permission
    vocabulary (`ALL_PERMISSIONS`). Mirrors `roleStore.refresh_role_cache`'s query."""
    async with role_manager.session_factory() as session:
        roles = (await session.execute(db.select(RoleSchema).where(
            RoleSchema.deleted_at.is_(None)))).scalars().all()
        perm_rows = (await session.execute(db.select(RolePermissionSchema).where(
            RolePermissionSchema.deleted_at.is_(None)))).scalars().all()

    by_role: dict = {}
    for rp in perm_rows:
        by_role.setdefault(rp.role_name, set()).add(rp.permission)

    items = [
        RoleItemResponse(
            name=r.name, scope_level=r.scope_level, location_type=r.location_type,
            is_system=r.is_system, perms=sorted(by_role.get(r.name, set())),
        )
        for r in roles
    ]
    return RolesCatalogResponse(roles=items, permissions=sorted(ALL_PERMISSIONS))


@router.post("", response_model=RoleItemResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    """Create a CUSTOM role (is_system=False)."""
    try:
        validate_new_role_name(payload.name)
        validate_scope_level(payload.scope_level)
        validate_role_perms(payload.perms)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    async with role_manager.session_factory() as session:
        existing = (await session.execute(db.select(RoleSchema).where(
            RoleSchema.name == payload.name))).scalars().first()
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"role already exists: {payload.name}")

        session.add(RoleSchema(
            name=payload.name, scope_level=payload.scope_level,
            location_type=payload.location_type, is_system=False,
            description=f"Custom role: {payload.name}",
        ))
        for perm in payload.perms:
            session.add(RolePermissionSchema(role_name=payload.name, permission=perm))
        await session.commit()

    await roleStore.refresh_role_cache(engine)

    await activity_manager.create(ActivityLogSchema(
        user_id=ctx.user_id, action="role.create", entity_type="role",
        entity_id=payload.name,
        details={"after": sorted(payload.perms), "scope_level": payload.scope_level},
        ip_address=_client_ip(request),
    ))

    return RoleItemResponse(
        name=payload.name, scope_level=payload.scope_level,
        location_type=payload.location_type, is_system=False,
        perms=sorted(payload.perms),
    )


@router.patch("/{name}", response_model=RoleItemResponse)
async def update_role_perms(
    name: str,
    payload: RolePermsUpdateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    """Edit a role's PERMS ONLY (no scope edit). `payload.perms` is the desired
    FULL perm set; diffed against current `role_permissions` rows. `is_system`
    roles ARE editable this way — only SUPER_ADMIN is untouchable."""
    if name == "SUPER_ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="SUPER_ADMIN is untouchable")

    try:
        validate_role_perms(payload.perms)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    async with role_manager.session_factory() as session:
        role = (await session.execute(db.select(RoleSchema).where(
            RoleSchema.name == name, RoleSchema.deleted_at.is_(None)))).scalars().first()
        if not role:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"role not found: {name}")

        perm_rows = (await session.execute(db.select(RolePermissionSchema).where(
            RolePermissionSchema.role_name == name,
            RolePermissionSchema.deleted_at.is_(None)))).scalars().all()
        current = {rp.permission for rp in perm_rows}
        desired = set(payload.perms)
        to_add, to_remove = diff_perms(desired, current)

        for perm in to_add:
            session.add(RolePermissionSchema(role_name=name, permission=perm))
        for rp in perm_rows:
            if rp.permission in to_remove:
                await session.delete(rp)
        await session.commit()

        scope_level, location_type, is_system = role.scope_level, role.location_type, role.is_system

    await roleStore.refresh_role_cache(engine)

    await activity_manager.create(ActivityLogSchema(
        user_id=ctx.user_id, action="role.update", entity_type="role", entity_id=name,
        details={"before": sorted(current), "after": sorted(desired)},
        ip_address=_client_ip(request),
    ))

    return RoleItemResponse(
        name=name, scope_level=scope_level, location_type=location_type,
        is_system=is_system, perms=sorted(desired),
    )
