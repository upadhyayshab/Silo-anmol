"""DB-backed role store — the runtime source of truth for RBAC roles.

Two jobs, both run once at startup (`load_roles`):
  1. `seed_default_roles` — bootstrap-only. Inserts a system role (`utils.permissions.
     ROLE_DEFINITIONS`) into the `roles` / `role_permissions` tables ONLY if it is missing.
     A role that already exists in the DB is left completely untouched (scope, location,
     and permissions included) — the DB is authoritative at runtime, so a superadmin's live
     edits survive the next deploy/restart instead of being re-synced away.
  2. `refresh_role_cache` — load every role + its permissions from the DB into the resolver
     cache (`utils.permissions.set_role_cache`), which `has_permission`/`apply_scope`/masking read.

Code dict = seed + fallback, applied once to a fresh DB; the DB tables are authoritative at
runtime thereafter, so an admin can add/edit a role without a deploy (call `refresh_role_cache`
after the write to publish it).
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


def _roles_to_bootstrap(defined_names: set, existing_names: set) -> set:
    """System role names present in code but missing from the DB — the only rows we insert.

    Pure/DB-free so it's unit-testable on its own (see tests/test_role_store.py)."""
    return defined_names - existing_names


async def seed_default_roles(engine) -> None:
    """Bootstrap-only seed of ROLE_DEFINITIONS into roles/role_permissions.

    Inserts a system role (row + perms) ONLY if it doesn't exist yet in the DB. A role
    that's already present is left entirely alone — no scope/location overwrite, no perm
    add, no perm prune — so a superadmin's live DB edits aren't wiped on the next boot.

    Accepted trade-off: a permission added to an EXISTING role in code will NOT
    auto-propagate to an already-seeded DB row; it must be toggled via the roles admin
    UI (later task) or a one-off script. New role *types* still seed (they're missing),
    and new *permission types* still surface automatically via ALL_PERMISSIONS.
    """
    async with RoleManager(engine).session_factory() as session:
        existing = {r.name: r for r in
                    (await session.execute(db.select(RoleSchema))).scalars().all()}
        defined_names = {role.value for role in ROLE_DEFINITIONS}
        to_bootstrap = _roles_to_bootstrap(defined_names, set(existing))

        for role, d in ROLE_DEFINITIONS.items():
            name = role.value
            if name not in to_bootstrap:
                continue    # already exists — DB is authoritative, leave it untouched

            scope = d["scope"].value if hasattr(d["scope"], "value") else str(d["scope"])
            loc = d.get("location_type")
            session.add(RoleSchema(name=name, scope_level=scope, location_type=loc,
                                   is_system=True, description=f"System role: {name}"))
            for perm in _perm_strs(d):
                session.add(RolePermissionSchema(role_name=name, permission=perm))
        await session.commit()
    logger.info(f"[rbac] bootstrapped {len(to_bootstrap)} missing system roles "
                f"(of {len(ROLE_DEFINITIONS)} defined)")


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
