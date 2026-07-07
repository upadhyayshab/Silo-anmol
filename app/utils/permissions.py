"""
RBAC permission registry — capability vocabulary, scope levels, role definitions
and column-masking config.

This module is the in-code SEED / source of truth for the DEFAULT roles, per
`RBAC_SCALABLE_DESIGN.md` (§3.1–3.2) and `RBAC_ACCESS_BLUEPRINT.md`. It only
*defines* the access model — enforcement lives in `utils/auth.py`
(`require_permission` / `apply_scope` / `apply_field_mask`) and is wired into
endpoints incrementally, so adding this module breaks nothing on its own.

Three orthogonal axes:
  - capability  -> Permission   (what action: "<resource>:<action>")
  - data scope  -> ScopeLevel   (which rows: GLOBAL/STATE/CLUSTER/OUTLET)
  - sensitivity -> MASKED_COLUMNS (which columns, e.g. cost/margin, tax ids)
"""
import re
from enum import Enum
from typing import Optional

from utils.constants import UserRole


# Wildcard capability — super admin holds this and passes every permission check.
WILDCARD = "*"


class Permission(str, Enum):
    """Canonical capability vocabulary. Seeds the SharedBackend `scopes` table."""
    # Orders
    ORDERS_READ = "orders:read"
    ORDERS_WRITE = "orders:write"
    ORDERS_STATUS = "orders:status"     # fulfillment state machine + bulk rider-assign
    ORDERS_MANAGE = "orders:manage"     # admin order ops (proxy/full-edit/reassign/txn-edit/delete)
    ORDERS_REVOKE = "orders:revoke"     # destructive un-deliver; SUPER-only (no explicit holder)
    # Products / catalogue
    PRODUCTS_READ = "products:read"
    PRODUCTS_WRITE = "products:write"
    PRODUCTS_COST_READ = "products:cost:read"     # gates masked cost/margin/commission
    PRODUCTS_COST_WRITE = "products:cost:write"    # editing cost/margin = finance only
    # Inventory / transfers
    INVENTORY_READ = "inventory:read"
    INVENTORY_WRITE = "inventory:write"          # routine order-flow stock moves (reserve/release/consume)
    INVENTORY_ADJUST = "inventory:adjust"        # privileged manual stock override — admin-tier only
    TRANSFERS_READ = "transfers:read"
    TRANSFERS_WRITE = "transfers:write"
    # Invoices / money
    INVOICES_READ = "invoices:read"
    INVOICES_WRITE = "invoices:write"
    COLLECTIONS_READ = "collections:read"
    COLLECTIONS_WRITE = "collections:write"
    TRANSACTIONS_READ = "transactions:read"
    TRANSACTIONS_WRITE = "transactions:write"
    # Payouts (maker-checker)
    PAYOUTS_READ = "payouts:read"
    PAYOUTS_WRITE = "payouts:write"        # propose / record
    PAYOUTS_APPROVE = "payouts:approve"    # finance-admin / L5
    PAYOUTS_READ_OWN = "payouts:read:own"  # everyone may see their OWN payout
    DRIVER_PAY_WRITE = "driver_pay:write"  # delivery-driver salary structure (L4 finance)
    # Finance overview / reports
    FINANCE_READ = "finance:read"          # company financials / margin zone
    REPORTS_READ = "reports:read"
    # Delivery (driver roster) / cash handovers / notifications
    DELIVERY_READ = "delivery:read"
    DELIVERY_WRITE = "delivery:write"
    HANDOVERS_READ = "handovers:read"
    HANDOVERS_WRITE = "handovers:write"
    HANDOVERS_READ_OWN = "handovers:read:own"    # delivery guy: view OWN cash balance + handover history only
    HANDOVERS_WRITE_OWN = "handovers:write:own"  # delivery guy: record OWN handover (create->PENDING only; NOT confirm/reject)
    NOTIFICATIONS_WRITE = "notifications:write"
    # Marketing / CRM
    CAMPAIGNS_READ = "campaigns:read"
    CAMPAIGNS_WRITE = "campaigns:write"
    LEADS_READ = "leads:read"
    LEADS_WRITE = "leads:write"
    LEADS_MANAGE = "leads:manage"       # admin lead ops: import / distribute / delete
    FACEBOOK_ADMIN = "facebook:admin"   # FB pages/forms/mappings/translations admin
    # Outlets / org
    OUTLETS_READ = "outlets:read"
    OUTLETS_TAXID_READ = "outlets:taxid:read"  # gates masked gstin/pan
    OUTLETS_WRITE = "outlets:write"
    CLUSTERS_READ = "clusters:read"
    CLUSTERS_WRITE = "clusters:write"
    # Admin / governance
    USERS_READ = "users:read"
    USERS_MANAGE = "users:manage"
    CONFIG_READ = "config:read"
    CONFIG_WRITE = "config:write"
    AUDIT_READ = "audit:read"
    ROLES_MANAGE = "roles:manage"   # superadmin-only; gates the roles admin API


class ScopeLevel(str, Enum):
    GLOBAL = "GLOBAL"
    STATE = "STATE"
    CLUSTER = "CLUSTER"
    OUTLET = "OUTLET"
    AGENCY = "AGENCY"


# ---------------------------------------------------------------------------
# Column masking: resource -> { gating_permission: [columns stripped without it] }
# A viewer who lacks the gating permission gets those columns nulled in the
# response (see utils.auth.apply_field_mask). This is the "finance veil" at the
# column level — the same /products payload, cost/margin removed below L4.
# ---------------------------------------------------------------------------
MASKED_COLUMNS = {
    "products": {
        # cost_price is the customer-facing selling price (MRP the order charges), so
        # order-takers (telecallers) must see it; only margin & commission stay finance-only.
        Permission.PRODUCTS_COST_READ: ["margin", "commission"],
    },
    "outlets": {
        Permission.OUTLETS_TAXID_READ: ["gstin", "pan"],
    },
    "orders": {
        Permission.PRODUCTS_COST_READ: ["total_commission"],
    },
}


P = Permission

# ---------------------------------------------------------------------------
# Default role definitions — the SEED. `perms` is a set of Permission (or the
# WILDCARD string). `scope` is the row-scope level; `location_type` pins a role
# to a warehouse/factory when set. Admin-composed custom roles live in the DB
# (later phase); these reproduce + extend today's behaviour.
#
# NOTE: `users.role` is a plain String(64) column, not a Postgres ENUM (see
# `managers/erpManagers.py:335`), so any role name — built-in or a custom,
# admin-created one — is assignable to a user with NO migration. Existence is
# validated at the app layer (against the `roles` table / this dict), not by
# the database schema.
# ---------------------------------------------------------------------------
ROLE_DEFINITIONS = {
    # ----- L5: super admin (everything, global) -----
    UserRole.SUPER_ADMIN: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={WILDCARD}),

    # ----- L1: ground (read + own tasks) -----
    UserRole.DELIVERY_GUY: dict(scope=ScopeLevel.OUTLET, location_type=None, perms={
        P.ORDERS_READ, P.TRANSFERS_READ, P.PAYOUTS_READ_OWN,
        # Delivery-captain app: view OWN cash balance + handover history AND record OWN handover
        # (self-scoped, not the outlet's). write:own creates a PENDING record; it can't confirm/reject.
        P.HANDOVERS_READ_OWN, P.HANDOVERS_WRITE_OWN,
    }),
    UserRole.VIEWER: dict(scope=ScopeLevel.OUTLET, location_type=None, perms={
        P.ORDERS_READ, P.INVENTORY_READ, P.PRODUCTS_READ,
    }),

    # ----- L2: operate (limited write, scoped) -----
    UserRole.TELECALLER: dict(scope=ScopeLevel.OUTLET, location_type=None, perms={
        P.ORDERS_READ, P.ORDERS_WRITE, P.PRODUCTS_READ, P.INVENTORY_READ,
        P.LEADS_READ, P.LEADS_WRITE, P.PAYOUTS_READ_OWN,
        # Step 5 (finance): legacy let telecaller record/read order payments for own orders (ownership enforced inline).
        P.TRANSACTIONS_READ, P.TRANSACTIONS_WRITE,
    }),
    UserRole.MARKETING_EXECUTIVE: dict(scope=ScopeLevel.CLUSTER, location_type=None, perms={
        P.CAMPAIGNS_READ, P.CAMPAIGNS_WRITE, P.REPORTS_READ, P.LEADS_READ,
    }),

    # ----- L3: manage (ops write, NO finance) -----
    UserRole.OUTLET_MANAGER: dict(scope=ScopeLevel.OUTLET, location_type=None, perms={
        P.ORDERS_READ, P.ORDERS_WRITE, P.ORDERS_STATUS, P.PRODUCTS_READ, P.OUTLETS_READ, P.OUTLETS_TAXID_READ,
        P.INVENTORY_READ, P.INVENTORY_WRITE, P.TRANSFERS_READ, P.TRANSFERS_WRITE,
        P.INVOICES_READ, P.INVOICES_WRITE, P.COLLECTIONS_READ, P.COLLECTIONS_WRITE,
        P.TRANSACTIONS_READ, P.REPORTS_READ, P.USERS_READ, P.PAYOUTS_READ_OWN, P.CONFIG_READ,
        # Batch 4d-2: outlet mgr sees its delivery roster + manages its cash handovers.
        P.DELIVERY_READ, P.HANDOVERS_READ, P.HANDOVERS_WRITE,
        # Step 5 (finance): legacy let OM record/update order payments + read outlet/rider payouts (own outlet via apply_scope).
        P.TRANSACTIONS_WRITE, P.PAYOUTS_READ,
    }),
    # GLOBAL scope (not OUTLET): warehouse managers operate across the network — they
    # fulfil transfers between locations and need all-outlet stock visibility (user
    # decision 2026-06-29). Their perms are limited to inventory/products/transfers/
    # outlets/reports (no orders, finance, or users), so GLOBAL here = "all outlets",
    # not broad sensitive access. location_type kept as metadata (apply_scope ignores it).
    UserRole.WAREHOUSE_MANAGER: dict(scope=ScopeLevel.GLOBAL, location_type="warehouse", perms={
        P.PRODUCTS_READ, P.PRODUCTS_WRITE, P.INVENTORY_READ, P.INVENTORY_WRITE,
        P.TRANSFERS_READ, P.TRANSFERS_WRITE, P.OUTLETS_READ, P.OUTLETS_TAXID_READ, P.REPORTS_READ,
        P.ORDERS_STATUS,    # bulk rider-assign (it was in that legacy list)
        P.DELIVERY_READ,    # Batch 4d-2: view delivery-guy roster (was in legacy delivery:read list)
    }),
    UserRole.CLUSTER_MANAGER: dict(scope=ScopeLevel.CLUSTER, location_type=None, perms={
        P.ORDERS_READ, P.ORDERS_WRITE, P.ORDERS_STATUS, P.PRODUCTS_READ, P.INVENTORY_READ,
        P.TRANSFERS_READ, P.TRANSFERS_WRITE, P.INVOICES_READ, P.COLLECTIONS_READ,
        P.REPORTS_READ, P.OUTLETS_READ, P.OUTLETS_TAXID_READ, P.CLUSTERS_READ, P.USERS_READ,
    }),
    # ADMIN umbrella — today's thin ops admin (transfers + inventory + catalogue).
    # Function-specific admin flavours are the *_ADMIN roles below.
    UserRole.ADMIN: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.PRODUCTS_READ, P.PRODUCTS_WRITE, P.INVENTORY_READ, P.INVENTORY_WRITE, P.INVENTORY_ADJUST,
        P.TRANSFERS_READ, P.TRANSFERS_WRITE, P.OUTLETS_READ, P.OUTLETS_TAXID_READ, P.OUTLETS_WRITE,
        P.CLUSTERS_READ, P.CLUSTERS_WRITE, P.REPORTS_READ, P.ORDERS_READ, P.ORDERS_WRITE, P.ORDERS_STATUS, P.ORDERS_MANAGE,
        P.COLLECTIONS_READ,
        P.CONFIG_READ, P.CONFIG_WRITE, P.AUDIT_READ,
        P.USERS_READ, P.USERS_MANAGE,   # 4d-2: USERS_READ so gating user list/get on users:read keeps ADMIN
        # Batch 4d-2: driver roster + cash handovers + admin notifications; FINANCE_READ
        # so ADMIN keeps the cost/profit reports now gated behind the finance veil.
        P.DELIVERY_READ, P.DELIVERY_WRITE, P.HANDOVERS_READ, P.HANDOVERS_WRITE,
        P.NOTIFICATIONS_WRITE, P.FINANCE_READ,
        # Batch 4d-4 (CRM dev-only): ADMIN gains full CRM lead access + FB config admin.
        # SUPER_ADMIN already covers these via WILDCARD.
        P.LEADS_READ, P.LEADS_WRITE, P.LEADS_MANAGE, P.FACEBOOK_ADMIN,
        # Step 5 (finance): legacy require_roles put ADMIN on every finance endpoint; restore ADMIN as
        # operational super-user + satisfy the cost-write/driver-pay/approve gates chosen by the user.
        P.INVOICES_READ, P.INVOICES_WRITE, P.TRANSACTIONS_READ, P.TRANSACTIONS_WRITE,
        P.COLLECTIONS_WRITE, P.PAYOUTS_READ, P.PAYOUTS_WRITE, P.PAYOUTS_APPROVE,
        P.PRODUCTS_COST_WRITE, P.DRIVER_PAY_WRITE,
    }),

    # ----- L4: oversee (read-heavy; accountant/finance write) -----
    UserRole.ACCOUNTANT: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.FINANCE_READ, P.REPORTS_READ,
        P.INVOICES_READ, P.COLLECTIONS_READ, P.COLLECTIONS_WRITE,
        P.TRANSACTIONS_READ, P.TRANSACTIONS_WRITE,
        P.PAYOUTS_READ, P.PAYOUTS_WRITE, P.DRIVER_PAY_WRITE,
        P.PAYOUTS_APPROVE,   # Step 5 (finance): user decision — keep ACCOUNTANT approving payouts (no maker-checker split).
        P.PRODUCTS_READ, P.PRODUCTS_COST_READ, P.OUTLETS_READ, P.OUTLETS_TAXID_READ,
        P.INVENTORY_READ,   # L4 oversight: accountant views inventory (blueprint §5)
        P.PAYOUTS_READ_OWN, P.CONFIG_READ, P.AUDIT_READ,
        # Batch 4d-2: accountant confirms/rejects delivery-guy cash handovers.
        P.HANDOVERS_READ, P.HANDOVERS_WRITE,
    }),
    UserRole.FINANCE_LEAD: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.FINANCE_READ, P.REPORTS_READ, P.INVOICES_READ, P.COLLECTIONS_READ,
        P.TRANSACTIONS_READ, P.PAYOUTS_READ, P.PRODUCTS_READ, P.PRODUCTS_COST_READ,
        P.OUTLETS_READ, P.OUTLETS_TAXID_READ,
    }),
    UserRole.STATE_HEAD: dict(scope=ScopeLevel.STATE, location_type=None, perms={
        P.ORDERS_READ, P.INVENTORY_READ, P.PRODUCTS_READ, P.REPORTS_READ,
        P.INVOICES_READ, P.COLLECTIONS_READ, P.TRANSFERS_READ, P.OUTLETS_READ,
        P.OUTLETS_TAXID_READ, P.CLUSTERS_READ, P.USERS_READ,
    }),
    UserRole.MARKETING_HEAD: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.CAMPAIGNS_READ, P.CAMPAIGNS_WRITE, P.REPORTS_READ, P.LEADS_READ,
    }),
    UserRole.AUDITOR: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.ORDERS_READ, P.INVENTORY_READ, P.PRODUCTS_READ, P.PRODUCTS_COST_READ,
        P.INVOICES_READ, P.COLLECTIONS_READ, P.TRANSACTIONS_READ, P.PAYOUTS_READ,
        P.FINANCE_READ, P.REPORTS_READ, P.AUDIT_READ, P.OUTLETS_READ, P.OUTLETS_TAXID_READ,
        P.CONFIG_READ, P.USERS_READ,
    }),

    # ----- Leadership (read-only, global, function-filtered) -----
    UserRole.CEO: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.ORDERS_READ, P.INVENTORY_READ, P.PRODUCTS_READ, P.PRODUCTS_COST_READ,
        P.INVOICES_READ, P.COLLECTIONS_READ, P.TRANSACTIONS_READ, P.PAYOUTS_READ,
        P.FINANCE_READ, P.REPORTS_READ, P.CAMPAIGNS_READ, P.LEADS_READ,
        P.OUTLETS_READ, P.OUTLETS_TAXID_READ, P.CLUSTERS_READ, P.USERS_READ, P.AUDIT_READ,
    }),
    UserRole.CFO: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.FINANCE_READ, P.REPORTS_READ, P.INVOICES_READ, P.COLLECTIONS_READ,
        P.TRANSACTIONS_READ, P.PAYOUTS_READ, P.PRODUCTS_READ, P.PRODUCTS_COST_READ,
        P.OUTLETS_READ, P.OUTLETS_TAXID_READ, P.CONFIG_READ,
    }),
    UserRole.COO: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.ORDERS_READ, P.INVENTORY_READ, P.PRODUCTS_READ, P.TRANSFERS_READ,
        P.INVOICES_READ, P.COLLECTIONS_READ, P.REPORTS_READ, P.OUTLETS_READ,
        P.OUTLETS_TAXID_READ, P.CLUSTERS_READ, P.USERS_READ,
    }),
    # CGO / growth head: top-line growth only — NO cost/margin, NO payouts/pay.
    UserRole.CGO: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.CAMPAIGNS_READ, P.LEADS_READ, P.ORDERS_READ, P.REPORTS_READ,
        P.PRODUCTS_READ, P.OUTLETS_READ,
    }),

    # ----- Admin flavours (function-scoped write/admin — slices of super admin) -----
    UserRole.USER_ADMIN: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.USERS_READ, P.USERS_MANAGE,
    }),
    UserRole.CATALOGUE_ADMIN: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.PRODUCTS_READ, P.PRODUCTS_WRITE,  # NOT cost/margin — that's finance
    }),
    UserRole.CONFIG_ADMIN: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.CONFIG_READ, P.CONFIG_WRITE, P.OUTLETS_READ, P.OUTLETS_TAXID_READ,
    }),
    UserRole.OPS_ADMIN: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.INVENTORY_READ, P.INVENTORY_WRITE, P.INVENTORY_ADJUST, P.TRANSFERS_READ, P.TRANSFERS_WRITE,
        P.CLUSTERS_READ, P.CLUSTERS_WRITE, P.OUTLETS_READ,
        P.ORDERS_READ, P.ORDERS_STATUS,    # ops oversight + fulfillment
    }),
    UserRole.FINANCE_ADMIN: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.FINANCE_READ, P.REPORTS_READ, P.INVOICES_READ, P.INVOICES_WRITE,
        P.COLLECTIONS_READ, P.COLLECTIONS_WRITE, P.TRANSACTIONS_READ, P.TRANSACTIONS_WRITE,
        P.PAYOUTS_READ, P.PAYOUTS_WRITE, P.PAYOUTS_APPROVE, P.DRIVER_PAY_WRITE,
        P.PRODUCTS_READ, P.PRODUCTS_COST_READ, P.PRODUCTS_COST_WRITE,
        P.CONFIG_READ, P.CONFIG_WRITE, P.OUTLETS_READ, P.OUTLETS_TAXID_READ,
    }),

    # ----- External calling agencies -----
    UserRole.AGENCY_TELECALLER: dict(scope=ScopeLevel.OUTLET, location_type=None, perms={
        P.ORDERS_READ, P.ORDERS_WRITE, P.PRODUCTS_READ, P.LEADS_READ,
    }),
    UserRole.AGENCY_ADMIN: dict(scope=ScopeLevel.AGENCY, location_type=None, perms={
        P.ORDERS_READ, P.REPORTS_READ, P.LEADS_READ, P.USERS_READ, P.USERS_MANAGE,
    }),
}


# All canonical permission strings — used to seed the SharedBackend `scopes` table.
ALL_PERMISSIONS = [p.value for p in Permission]


# ---------------------------------------------------------------------------
# Runtime role store (DB-backed). `services.roleStore` seeds the default roles into
# the roles/role_permissions tables and loads them back here at startup. Until that
# load runs, the cache is empty and resolvers fall back to ROLE_DEFINITIONS (the code
# seed) — so sync scripts, tests, and pre-load requests still resolve the defaults.
# The DB is the runtime source of truth (lets admins add custom roles without a deploy);
# code remains the seed + fallback. Same shape as ROLE_DEFINITIONS, keyed by role name.
# ---------------------------------------------------------------------------
_ROLE_CACHE: dict = {}   # name(str) -> {"scope": ScopeLevel, "location_type": str|None, "perms": set[str]}


def set_role_cache(roles: dict) -> None:
    """Replace the in-process role cache (called by roleStore.refresh_role_cache)."""
    global _ROLE_CACHE
    _ROLE_CACHE = roles or {}


def _role_name(role) -> str:
    return role.value if isinstance(role, UserRole) else str(role)


# ---------------------------------------------------------------------------
# Resolver helpers (string-friendly: role may be a UserRole or its JWT string)
# ---------------------------------------------------------------------------
def _perm_value(perm) -> str:
    return perm.value if isinstance(perm, Permission) else str(perm)


def get_role_def(role) -> Optional[dict]:
    """Role's {scope, location_type, perms}. DB cache first (runtime source of truth),
    then the in-code ROLE_DEFINITIONS seed (before the cache loads, or for a role the
    DB doesn't carry)."""
    cached = _ROLE_CACHE.get(_role_name(role))
    if cached:
        return cached
    if isinstance(role, UserRole):
        return ROLE_DEFINITIONS.get(role)
    try:
        return ROLE_DEFINITIONS.get(UserRole(role))
    except ValueError:
        return None


def role_perms(role) -> set:
    """Resolved set of permission strings for a role (or empty set if unknown)."""
    d = get_role_def(role)
    if not d:
        return set()
    return {_perm_value(p) for p in d["perms"]}


def role_scope_level(role) -> ScopeLevel:
    d = get_role_def(role)
    return d["scope"] if d else ScopeLevel.OUTLET


def has_permission(role, perm) -> bool:
    perms = role_perms(role)
    if WILDCARD in perms:
        return True
    return _perm_value(perm) in perms


def masked_columns_for(resource: str, role) -> list:
    """Columns to strip from a `resource` response for this role (none if wildcard)."""
    perms = role_perms(role)
    if WILDCARD in perms:
        return []
    cols = []
    for gating_perm, columns in MASKED_COLUMNS.get(resource, {}).items():
        if _perm_value(gating_perm) not in perms:
            cols.extend(columns)
    return cols


# ---------------------------------------------------------------------------
# Roles-admin guardrails (DB-free, pure). Used by routers/v1/roles.py to
# validate create/edit requests before touching the DB; kept here so they're
# importable + unit-testable without a DB (see tests/test_role_admin.py).
# ---------------------------------------------------------------------------
ROLE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def validate_role_perms(perms: list) -> None:
    """Every perm must be in ALL_PERMISSIONS; WILDCARD ("*") is rejected — no
    privilege escalation via the roles admin API (wildcard stays SUPER_ADMIN-only).
    Empty list is fine."""
    for p in perms:
        if p == WILDCARD:
            raise ValueError("wildcard permission (\"*\") cannot be granted via the roles admin API")
        if p not in ALL_PERMISSIONS:
            raise ValueError(f"unknown permission: {p}")


def validate_scope_level(scope: str) -> None:
    """`scope` must be a valid ScopeLevel value."""
    try:
        ScopeLevel(scope)
    except ValueError:
        raise ValueError(f"invalid scope_level: {scope}")


def validate_new_role_name(name: str) -> None:
    """New role name must match ROLE_NAME_RE (uppercase slug). Uses `re.fullmatch`
    (not `.match`) so a trailing newline (e.g. "AB\n") — which `$` alone would
    accept — is rejected too."""
    if not re.fullmatch(ROLE_NAME_RE, name or ""):
        raise ValueError(
            f"invalid role name: {name!r} (must match ^[A-Z][A-Z0-9_]{{1,63}}$)"
        )


def diff_perms(desired: set, current: set) -> tuple:
    """Pure set-diff for a PATCH perms update. Returns (to_add, to_remove)."""
    return desired - current, current - desired


__all__ = [
    "Permission", "ScopeLevel", "WILDCARD", "MASKED_COLUMNS", "ROLE_DEFINITIONS",
    "ALL_PERMISSIONS", "get_role_def", "role_perms", "role_scope_level",
    "has_permission", "masked_columns_for", "set_role_cache",
    "ROLE_NAME_RE", "validate_role_perms", "validate_scope_level",
    "validate_new_role_name", "diff_perms",
]
