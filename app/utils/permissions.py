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
    # Products / catalogue
    PRODUCTS_READ = "products:read"
    PRODUCTS_WRITE = "products:write"
    PRODUCTS_COST_READ = "products:cost:read"     # gates masked cost/margin/commission
    PRODUCTS_COST_WRITE = "products:cost:write"    # editing cost/margin = finance only
    # Inventory / transfers
    INVENTORY_READ = "inventory:read"
    INVENTORY_WRITE = "inventory:write"
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
    # Marketing / CRM
    CAMPAIGNS_READ = "campaigns:read"
    CAMPAIGNS_WRITE = "campaigns:write"
    LEADS_READ = "leads:read"
    LEADS_WRITE = "leads:write"
    # Outlets / org
    OUTLETS_READ = "outlets:read"
    OUTLETS_TAXID_READ = "outlets:taxid:read"  # gates masked gstin/pan
    CLUSTERS_READ = "clusters:read"
    CLUSTERS_WRITE = "clusters:write"
    # Admin / governance
    USERS_READ = "users:read"
    USERS_MANAGE = "users:manage"
    CONFIG_READ = "config:read"
    CONFIG_WRITE = "config:write"
    AUDIT_READ = "audit:read"


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
        Permission.PRODUCTS_COST_READ: ["cost_price", "margin", "commission"],
    },
    "outlets": {
        Permission.OUTLETS_TAXID_READ: ["gstin", "pan"],
    },
}


P = Permission

# ---------------------------------------------------------------------------
# Default role definitions — the SEED. `perms` is a set of Permission (or the
# WILDCARD string). `scope` is the row-scope level; `location_type` pins a role
# to a warehouse/factory when set. Admin-composed custom roles live in the DB
# (later phase); these reproduce + extend today's behaviour.
#
# NOTE: new role *names* added to UserRole need a Postgres ENUM `ADD VALUE`
# migration before a user can be assigned one (Phase 2). Reading existing users
# is unaffected.
# ---------------------------------------------------------------------------
ROLE_DEFINITIONS = {
    # ----- L5: super admin (everything, global) -----
    UserRole.SUPER_ADMIN: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={WILDCARD}),

    # ----- L1: ground (read + own tasks) -----
    UserRole.DELIVERY_GUY: dict(scope=ScopeLevel.OUTLET, location_type=None, perms={
        P.ORDERS_READ, P.TRANSFERS_READ, P.PAYOUTS_READ_OWN,
    }),
    UserRole.VIEWER: dict(scope=ScopeLevel.OUTLET, location_type=None, perms={
        P.ORDERS_READ, P.INVENTORY_READ, P.PRODUCTS_READ,
    }),

    # ----- L2: operate (limited write, scoped) -----
    UserRole.TELECALLER: dict(scope=ScopeLevel.OUTLET, location_type=None, perms={
        P.ORDERS_READ, P.ORDERS_WRITE, P.PRODUCTS_READ, P.INVENTORY_READ,
        P.LEADS_READ, P.LEADS_WRITE, P.PAYOUTS_READ_OWN,
    }),
    UserRole.MARKETING_EXECUTIVE: dict(scope=ScopeLevel.CLUSTER, location_type=None, perms={
        P.CAMPAIGNS_READ, P.CAMPAIGNS_WRITE, P.REPORTS_READ, P.LEADS_READ,
    }),

    # ----- L3: manage (ops write, NO finance) -----
    UserRole.OUTLET_MANAGER: dict(scope=ScopeLevel.OUTLET, location_type=None, perms={
        P.ORDERS_READ, P.ORDERS_WRITE, P.PRODUCTS_READ, P.OUTLETS_READ, P.OUTLETS_TAXID_READ,
        P.INVENTORY_READ, P.INVENTORY_WRITE, P.TRANSFERS_READ, P.TRANSFERS_WRITE,
        P.INVOICES_READ, P.INVOICES_WRITE, P.COLLECTIONS_READ, P.COLLECTIONS_WRITE,
        P.TRANSACTIONS_READ, P.REPORTS_READ, P.USERS_READ, P.PAYOUTS_READ_OWN,
    }),
    UserRole.WAREHOUSE_MANAGER: dict(scope=ScopeLevel.OUTLET, location_type="warehouse", perms={
        P.PRODUCTS_READ, P.PRODUCTS_WRITE, P.INVENTORY_READ, P.INVENTORY_WRITE,
        P.TRANSFERS_READ, P.TRANSFERS_WRITE, P.OUTLETS_READ, P.OUTLETS_TAXID_READ, P.REPORTS_READ,
    }),
    UserRole.CLUSTER_MANAGER: dict(scope=ScopeLevel.CLUSTER, location_type=None, perms={
        P.ORDERS_READ, P.ORDERS_WRITE, P.PRODUCTS_READ, P.INVENTORY_READ,
        P.TRANSFERS_READ, P.TRANSFERS_WRITE, P.INVOICES_READ, P.COLLECTIONS_READ,
        P.REPORTS_READ, P.OUTLETS_READ, P.OUTLETS_TAXID_READ, P.CLUSTERS_READ, P.USERS_READ,
    }),
    # ADMIN umbrella — today's thin ops admin (transfers + inventory + catalogue).
    # Function-specific admin flavours are the *_ADMIN roles below.
    UserRole.ADMIN: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.PRODUCTS_READ, P.PRODUCTS_WRITE, P.INVENTORY_READ, P.INVENTORY_WRITE,
        P.TRANSFERS_READ, P.TRANSFERS_WRITE, P.OUTLETS_READ, P.OUTLETS_TAXID_READ,
        P.CLUSTERS_READ, P.REPORTS_READ, P.ORDERS_READ, P.COLLECTIONS_READ,
        P.USERS_MANAGE,
    }),

    # ----- L4: oversee (read-heavy; accountant/finance write) -----
    UserRole.ACCOUNTANT: dict(scope=ScopeLevel.GLOBAL, location_type=None, perms={
        P.FINANCE_READ, P.REPORTS_READ,
        P.INVOICES_READ, P.COLLECTIONS_READ, P.COLLECTIONS_WRITE,
        P.TRANSACTIONS_READ, P.TRANSACTIONS_WRITE,
        P.PAYOUTS_READ, P.PAYOUTS_WRITE, P.DRIVER_PAY_WRITE,
        P.PRODUCTS_READ, P.PRODUCTS_COST_READ, P.OUTLETS_READ, P.OUTLETS_TAXID_READ,
        P.PAYOUTS_READ_OWN,
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
        P.INVENTORY_READ, P.INVENTORY_WRITE, P.TRANSFERS_READ, P.TRANSFERS_WRITE,
        P.CLUSTERS_READ, P.CLUSTERS_WRITE, P.OUTLETS_READ,
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
# Resolver helpers (string-friendly: role may be a UserRole or its JWT string)
# ---------------------------------------------------------------------------
def _perm_value(perm) -> str:
    return perm.value if isinstance(perm, Permission) else str(perm)


def get_role_def(role) -> Optional[dict]:
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


__all__ = [
    "Permission", "ScopeLevel", "WILDCARD", "MASKED_COLUMNS", "ROLE_DEFINITIONS",
    "ALL_PERMISSIONS", "get_role_def", "role_perms", "role_scope_level",
    "has_permission", "masked_columns_for",
]
