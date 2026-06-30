"""DB-backed role store — the runtime source of truth for RBAC roles.

Two jobs, both run once at startup (`load_roles`):
  1. `seed_default_roles` — upsert the in-code defaults (`utils.permissions.ROLE_DEFINITIONS`)
     into the `roles` / `role_permissions` tables. Idempotent; the 25 system roles are kept
     in exact sync with code on every boot (perms reconciled add+prune). Custom, admin-created
     roles are left untouched.
  2. `refresh_role_cache` — load every role + its permissions from the DB into the resolver
     cache (`utils.permissions.set_role_cache`), which `has_permission`/`apply_scope`/masking read.

Code dict = seed + fallback; the DB tables are authoritative at runtime, so an admin can add
a role without a deploy (call `refresh_role_cache` after the write to publish it).
"""
import logging

import sqlalchemy as db

from managers import RoleManager, RoleSchema, RolePermissionSchema
from utils import permissions
from utils.permissions import ROLE_DEFINITIONS, ScopeLevel

logger = logging.getLogger(__name__)


def _perm_strs(role_def: dict) -> set:
    """The role's permissions as plain strings (Permission enum or WILDCARD '*')."""
    return {p.value if hasattr(p, "value") else str(p) for p in role_def["perms"]}


async def seed_default_roles(engine) -> None:
    """Upsert ROLE_DEFINITIONS into roles/role_permissions. Idempotent and self-healing:
    system roles' scope/location/perms are reconciled to match code on every startup."""
    async with RoleManager(engine).session_factory() as session:
        existing = {r.name: r for r in
                    (await session.execute(db.select(RoleSchema))).scalars().all()}
        for role, d in ROLE_DEFINITIONS.items():
            name = role.value
            scope = d["scope"].value if hasattr(d["scope"], "value") else str(d["scope"])
            loc = d.get("location_type")
            want = _perm_strs(d)

            row = existing.get(name)
            if row is None:
                session.add(RoleSchema(name=name, scope_level=scope, location_type=loc,
                                       is_system=True, description=f"System role: {name}"))
            else:
                row.scope_level, row.location_type, row.is_system = scope, loc, True

            current = {rp.permission: rp for rp in (await session.execute(
                db.select(RolePermissionSchema).where(
                    RolePermissionSchema.role_name == name))).scalars().all()}
            for perm in want - set(current):                 # add new
                session.add(RolePermissionSchema(role_name=name, permission=perm))
            for perm in set(current) - want:                 # prune removed (system roles only)
                await session.delete(current[perm])
        await session.commit()
    logger.info(f"[rbac] seeded {len(ROLE_DEFINITIONS)} system roles")


async def refresh_role_cache(engine) -> int:
    """Load roles + permissions from the DB into the resolver cache. Returns the count."""
    async with RoleManager(engine).session_factory() as session:
        roles = (await session.execute(db.select(RoleSchema).where(
            RoleSchema.deleted_at.is_(None)))).scalars().all()
        perm_rows = (await session.execute(db.select(RolePermissionSchema).where(
            RolePermissionSchema.deleted_at.is_(None)))).scalars().all()

    by_role: dict = {}
    for rp in perm_rows:
        by_role.setdefault(rp.role_name, set()).add(rp.permission)

    cache = {}
    for r in roles:
        try:
            scope = ScopeLevel(r.scope_level)
        except ValueError:
            scope = ScopeLevel.OUTLET
        cache[r.name] = {"scope": scope, "location_type": r.location_type,
                         "perms": by_role.get(r.name, set())}
    permissions.set_role_cache(cache)
    logger.info(f"[rbac] role cache loaded: {len(cache)} roles")
    return len(cache)


async def load_roles(engine) -> None:
    """Startup hook: seed the defaults, then publish the DB roles to the resolver cache.
    Best-effort — if the roles tables aren't migrated yet, log and leave the code-dict
    fallback in place rather than blocking app boot."""
    try:
        await seed_default_roles(engine)
        await refresh_role_cache(engine)
    except Exception as e:
        logger.warning(f"[rbac] role store init skipped ({e}); using in-code ROLE_DEFINITIONS")
