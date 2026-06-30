"""Guards Step 4 router conversions against access regressions (DB-free).

Each converted endpoint family records the roles that could reach it under the old
`require_roles` lists. After swapping to `require_permission`, every legacy role must
still hold the gating permission — EXCEPT the intentional, blueprint-driven drops listed
explicitly. This catches a seed edit silently locking someone out (or silently un-dropping
an intended removal). Extend per batch. Run::

    python tests/test_rbac_router_access.py   # or: pytest tests/test_rbac_router_access.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from utils.permissions import has_permission, set_role_cache  # noqa: E402

set_role_cache({})  # resolve against the code seed (ROLE_DEFINITIONS), not a loaded DB cache


# --- inventory.py (Step 4a) -------------------------------------------------
# Reads were gated by {OUTLET_MANAGER, WAREHOUSE_MANAGER, ADMIN, SUPER_ADMIN} (+ ACCOUNTANT
# on the main list). Now gated `inventory:read`. Writes were {SUPER_ADMIN, ADMIN,
# WAREHOUSE_MANAGER, OUTLET_MANAGER} (+ TELECALLER on the dead /reserve). Now `inventory:write`.
INVENTORY_READ_LEGACY = {"OUTLET_MANAGER", "WAREHOUSE_MANAGER", "ADMIN", "SUPER_ADMIN", "ACCOUNTANT"}
# Routine order-flow writes (/reserve, /release, /consume) -> inventory:write.
INVENTORY_WRITE_LEGACY = {"SUPER_ADMIN", "ADMIN", "WAREHOUSE_MANAGER", "OUTLET_MANAGER"}
INVENTORY_WRITE_INTENTIONAL_DROPS = {"TELECALLER"}  # only on the no-op /reserve
# Manual stock override (/adjust) is admin-tier only (user decision 2026-06-29).
INVENTORY_ADJUST_ALLOWED = {"SUPER_ADMIN", "ADMIN", "OPS_ADMIN"}
INVENTORY_ADJUST_INTENTIONAL_DROPS = {"WAREHOUSE_MANAGER", "OUTLET_MANAGER"}  # had it pre-conversion


def test_inventory_read_no_regression():
    for role in INVENTORY_READ_LEGACY:
        assert has_permission(role, "inventory:read"), f"{role} lost inventory:read"


def test_inventory_write_no_regression():
    for role in INVENTORY_WRITE_LEGACY:
        assert has_permission(role, "inventory:write"), f"{role} lost inventory:write"


def test_inventory_write_intentional_drops():
    for role in INVENTORY_WRITE_INTENTIONAL_DROPS:
        assert not has_permission(role, "inventory:write"), \
            f"{role} unexpectedly still holds inventory:write (intended to be dropped)"


def test_inventory_adjust_is_admin_tier():
    for role in INVENTORY_ADJUST_ALLOWED:
        assert has_permission(role, "inventory:adjust"), f"{role} should hold inventory:adjust"
    for role in INVENTORY_ADJUST_INTENTIONAL_DROPS:
        assert not has_permission(role, "inventory:adjust"), \
            f"{role} should NOT hold inventory:adjust (admin-tier only)"


def test_warehouse_is_global_for_all_outlet_visibility():
    # Decision 2026-06-29: warehouse managers see all outlets' stock.
    from utils.permissions import role_scope_level, ScopeLevel
    assert role_scope_level("WAREHOUSE_MANAGER") == ScopeLevel.GLOBAL


# --- transfers.py (Step 4b) -------------------------------------------------
# Reads (list, get, pending-approvals, summary) were gated by {OUTLET_MANAGER,
# WAREHOUSE_MANAGER, ADMIN, SUPER_ADMIN}; now `transfers:read`. Writes (create,
# mass-upload, approve, status, approve-with-quantities) were gated by the same set
# (+ none extra); now `transfers:write`. Scoped callers are row-fenced by a from/to-outlet
# OR; GLOBAL roles (warehouse/admin/ops/super) are unrestricted.
TRANSFERS_READ_LEGACY = {"OUTLET_MANAGER", "WAREHOUSE_MANAGER", "ADMIN", "SUPER_ADMIN"}
TRANSFERS_WRITE_LEGACY = {"OUTLET_MANAGER", "WAREHOUSE_MANAGER", "ADMIN", "SUPER_ADMIN"}
# Roles newly *gaining* transfer access under the blueprint (broadening, all row-scoped
# except the GLOBAL ones). Pinned so an accidental seed change is caught either way.
TRANSFERS_READ_BROADENED = {"DELIVERY_GUY", "CLUSTER_MANAGER", "STATE_HEAD", "COO", "OPS_ADMIN"}
TRANSFERS_WRITE_BROADENED = {"CLUSTER_MANAGER", "OPS_ADMIN"}


def test_transfers_read_no_regression():
    for role in TRANSFERS_READ_LEGACY:
        assert has_permission(role, "transfers:read"), f"{role} lost transfers:read"


def test_transfers_write_no_regression():
    for role in TRANSFERS_WRITE_LEGACY:
        assert has_permission(role, "transfers:write"), f"{role} lost transfers:write"


def test_transfers_read_broadened():
    for role in TRANSFERS_READ_BROADENED:
        assert has_permission(role, "transfers:read"), f"{role} should hold transfers:read"


def test_transfers_write_broadened():
    for role in TRANSFERS_WRITE_BROADENED:
        assert has_permission(role, "transfers:write"), f"{role} should hold transfers:write"
    # Read-only newcomers must NOT have gained write.
    for role in {"DELIVERY_GUY", "STATE_HEAD", "COO"}:
        assert not has_permission(role, "transfers:write"), \
            f"{role} unexpectedly holds transfers:write (read-only role)"


# --- orders.py (Step 4c) ----------------------------------------------------
# Reads (list, get, by-phone, counts) were reachable by the legacy order readers; now
# gated `orders:read`. Writes (create/update/payment/transactions) were the legacy writer
# set; now `orders:write`. NEW perms split the rest of the old role-list endpoints:
#   orders:status -> fulfillment state machine + bulk rider-assign (managers + ops + warehouse)
#   orders:manage -> admin order ops (proxy/full-edit/reassign/txn-edit/delete) = ADMIN(+SUPER)
#   orders:revoke -> destructive un-deliver = SUPER only (no explicit holder)
# Plus read broadenings for leadership/oversight roles now able to list (row-scoped/global).
ORDERS_READ_LEGACY = {"TELECALLER", "OUTLET_MANAGER", "ADMIN", "SUPER_ADMIN", "DELIVERY_GUY", "AGENCY_ADMIN"}
ORDERS_WRITE_LEGACY = {"TELECALLER", "OUTLET_MANAGER", "ADMIN", "SUPER_ADMIN", "AGENCY_TELECALLER"}
# orders:status holders per §B (managers + warehouse bulk rider-assign + ops oversight + admin).
ORDERS_STATUS_HOLDERS = {"OUTLET_MANAGER", "CLUSTER_MANAGER", "WAREHOUSE_MANAGER", "OPS_ADMIN", "ADMIN"}
# status is not a telecaller/delivery action.
ORDERS_STATUS_NON_HOLDERS = {"TELECALLER", "AGENCY_TELECALLER", "DELIVERY_GUY"}
# Admin order ops are ADMIN(+SUPER) only — managers/telecaller can't delete/reassign/full-edit.
ORDERS_MANAGE_HOLDERS = {"ADMIN", "SUPER_ADMIN"}
ORDERS_MANAGE_NON_HOLDERS = {"OUTLET_MANAGER", "CLUSTER_MANAGER", "TELECALLER"}
# Destructive un-deliver is SUPER-only (granted to no explicit role).
ORDERS_REVOKE_HOLDERS = {"SUPER_ADMIN"}
ORDERS_REVOKE_NON_HOLDERS = {"ADMIN", "OUTLET_MANAGER"}
# Roles newly able to list orders (row-scoped, or global for leadership/ops).
ORDERS_READ_BROADENED = {"STATE_HEAD", "AUDITOR", "CEO", "COO", "CGO", "OPS_ADMIN"}


def test_orders_read_no_regression():
    for role in ORDERS_READ_LEGACY:
        assert has_permission(role, "orders:read"), f"{role} lost orders:read"


def test_orders_write_no_regression():
    for role in ORDERS_WRITE_LEGACY:
        assert has_permission(role, "orders:write"), f"{role} lost orders:write"


def test_orders_status_holders():
    for role in ORDERS_STATUS_HOLDERS:
        assert has_permission(role, "orders:status"), f"{role} should hold orders:status"
    for role in ORDERS_STATUS_NON_HOLDERS:
        assert not has_permission(role, "orders:status"), \
            f"{role} unexpectedly holds orders:status (not a telecaller/delivery action)"


def test_orders_manage_is_admin_tier():
    for role in ORDERS_MANAGE_HOLDERS:
        assert has_permission(role, "orders:manage"), f"{role} should hold orders:manage"
    for role in ORDERS_MANAGE_NON_HOLDERS:
        assert not has_permission(role, "orders:manage"), \
            f"{role} unexpectedly holds orders:manage (admin-tier only)"


def test_orders_revoke_is_super_only():
    for role in ORDERS_REVOKE_HOLDERS:
        assert has_permission(role, "orders:revoke"), f"{role} should hold orders:revoke"
    for role in ORDERS_REVOKE_NON_HOLDERS:
        assert not has_permission(role, "orders:revoke"), \
            f"{role} unexpectedly holds orders:revoke (SUPER-only)"


def test_orders_read_broadened():
    for role in ORDERS_READ_BROADENED:
        assert has_permission(role, "orders:read"), f"{role} should hold orders:read"


# --- batch 4d-1 (geo/config/audit) ------------------------------------------
# outlets.py reads (get, list, collections/summary scope aside) were the legacy outlet
# readers; now `outlets:read`. TELECALLER was on the old list() role-list but is
# intentionally DROPPED (lacks outlets:read). Outlet writes (create/update/deactivate)
# + outlet_mapping writes were {SUPER,ADMIN}; now `outlets:write` (ADMIN+SUPER).
OUTLETS_READ_LEGACY = {"OUTLET_MANAGER", "WAREHOUSE_MANAGER", "ACCOUNTANT", "ADMIN", "SUPER_ADMIN"}
OUTLETS_READ_INTENTIONAL_DROPS = {"TELECALLER"}  # on old list() role-list, lacks outlets:read
OUTLETS_WRITE_HOLDERS = {"ADMIN", "SUPER_ADMIN"}
OUTLETS_WRITE_NON_HOLDERS = {"OUTLET_MANAGER", "WAREHOUSE_MANAGER"}
# clusters.py writes (create/put/delete/districts/outlets/bulk) were {SUPER,ADMIN}; now
# `clusters:write` (ADMIN, OPS_ADMIN, +SUPER). Geo readers keep read-only access.
CLUSTERS_WRITE_HOLDERS = {"ADMIN", "OPS_ADMIN", "SUPER_ADMIN"}
CLUSTERS_WRITE_NON_HOLDERS = {"CLUSTER_MANAGER", "STATE_HEAD"}  # read-only geo roles
CLUSTERS_READ_LEGACY = {"CLUSTER_MANAGER", "STATE_HEAD", "ADMIN", "OPS_ADMIN", "SUPER_ADMIN"}
# config.py GET was {SUPER,ADMIN,OUTLET_MANAGER,ACCOUNTANT}; now `config:read` (also resolvable
# by AUDITOR/CFO/FINANCE_ADMIN/CONFIG_ADMIN per blueprint). Writes were {SUPER,ADMIN}; now
# `config:write` (ADMIN, CONFIG_ADMIN, FINANCE_ADMIN, +SUPER).
CONFIG_READ_HOLDERS = {"ADMIN", "OUTLET_MANAGER", "ACCOUNTANT", "SUPER_ADMIN"}
CONFIG_WRITE_HOLDERS = {"ADMIN", "CONFIG_ADMIN", "FINANCE_ADMIN", "SUPER_ADMIN"}
CONFIG_WRITE_NON_HOLDERS = {"OUTLET_MANAGER"}
# activity_logs.py reads were {SUPER,ADMIN,ACCOUNTANT} (summary {SUPER,ADMIN}); now `audit:read`
# (ADMIN, ACCOUNTANT, AUDITOR, CEO, +SUPER).
AUDIT_READ_HOLDERS = {"ADMIN", "ACCOUNTANT", "AUDITOR", "CEO", "SUPER_ADMIN"}
AUDIT_READ_NON_HOLDERS = {"OUTLET_MANAGER", "TELECALLER"}


def test_outlets_read_no_regression():
    for role in OUTLETS_READ_LEGACY:
        assert has_permission(role, "outlets:read"), f"{role} lost outlets:read"
    for role in OUTLETS_READ_INTENTIONAL_DROPS:
        assert not has_permission(role, "outlets:read"), \
            f"{role} unexpectedly holds outlets:read (intended to be dropped)"


def test_outlets_write_holders():
    for role in OUTLETS_WRITE_HOLDERS:
        assert has_permission(role, "outlets:write"), f"{role} should hold outlets:write"
    for role in OUTLETS_WRITE_NON_HOLDERS:
        assert not has_permission(role, "outlets:write"), \
            f"{role} unexpectedly holds outlets:write (admin-tier only)"


def test_clusters_write_holders():
    for role in CLUSTERS_WRITE_HOLDERS:
        assert has_permission(role, "clusters:write"), f"{role} should hold clusters:write"
    for role in CLUSTERS_WRITE_NON_HOLDERS:
        assert not has_permission(role, "clusters:write"), \
            f"{role} unexpectedly holds clusters:write (read-only geo role)"


def test_clusters_read_no_regression():
    for role in CLUSTERS_READ_LEGACY:
        assert has_permission(role, "clusters:read"), f"{role} lost clusters:read"


def test_config_read_holders():
    for role in CONFIG_READ_HOLDERS:
        assert has_permission(role, "config:read"), f"{role} should hold config:read"


def test_config_write_holders():
    for role in CONFIG_WRITE_HOLDERS:
        assert has_permission(role, "config:write"), f"{role} should hold config:write"
    for role in CONFIG_WRITE_NON_HOLDERS:
        assert not has_permission(role, "config:write"), \
            f"{role} unexpectedly holds config:write (admin/config-tier only)"


def test_audit_read_holders():
    for role in AUDIT_READ_HOLDERS:
        assert has_permission(role, "audit:read"), f"{role} should hold audit:read"
    for role in AUDIT_READ_NON_HOLDERS:
        assert not has_permission(role, "audit:read"), \
            f"{role} unexpectedly holds audit:read (not an audit reader)"


# --- batch 4d-2 (ops + reporting) -------------------------------------------
# delivery_guys.py reads (get/list) -> `delivery:read`; writes (create/update/delete) ->
# `delivery:write` (ADMIN+SUPER). delivery_handovers.py -> `handovers:read`/`handovers:write`
# (ADMIN, OUTLET_MANAGER, ACCOUNTANT). notifications.py create -> `notifications:write`
# (ADMIN+SUPER). reports.py: cost/profit endpoints gated `finance:read` (ADMIN granted §A),
# operational/revenue endpoints `reports:read`. users.py reads -> `users:read` with three
# intentional drops. See §A holder sets + §D test bullets.
DELIVERY_READ_HOLDERS = {"ADMIN", "WAREHOUSE_MANAGER", "OUTLET_MANAGER", "SUPER_ADMIN"}
DELIVERY_WRITE_HOLDERS = {"ADMIN", "SUPER_ADMIN"}
DELIVERY_WRITE_NON_HOLDERS = {"OUTLET_MANAGER"}  # read-only delivery viewer
# handovers:read == handovers:write holder set (both ADMIN, OUTLET_MANAGER, ACCOUNTANT +SUPER).
HANDOVERS_HOLDERS = {"ADMIN", "OUTLET_MANAGER", "ACCOUNTANT", "SUPER_ADMIN"}
NOTIFICATIONS_WRITE_HOLDERS = {"ADMIN", "SUPER_ADMIN"}
NOTIFICATIONS_WRITE_NON_HOLDERS = {"OUTLET_MANAGER"}
# finance:read gates the cost/profit/margin reports; ADMIN is newly granted it (§A). The three
# operational roles intentionally LOSE the sensitive reports (they lack finance:read).
FINANCE_READ_HOLDERS = {"ACCOUNTANT", "FINANCE_ADMIN", "AUDITOR", "CEO", "CFO", "ADMIN", "SUPER_ADMIN"}
FINANCE_READ_NON_HOLDERS = {"OUTLET_MANAGER", "WAREHOUSE_MANAGER", "TELECALLER"}
# reports:read no-regression for operational (revenue) viewers; TELECALLER is the intentional
# drop from order-performance (lacks reports:read; keeps only its own /telecaller dashboard).
REPORTS_READ_OPERATIONAL_VIEWERS = {"OUTLET_MANAGER", "ACCOUNTANT", "ADMIN", "WAREHOUSE_MANAGER", "SUPER_ADMIN"}
REPORTS_READ_INTENTIONAL_DROPS = {"TELECALLER"}
# users.py reads -> users:read. Keepers preserve access; WAREHOUSE_MANAGER/TELECALLER/ACCOUNTANT
# are the intentional narrow (lose user read).
USERS_READ_KEEPERS = {"ADMIN", "OUTLET_MANAGER", "AGENCY_ADMIN", "SUPER_ADMIN"}
USERS_READ_INTENTIONAL_DROPS = {"WAREHOUSE_MANAGER", "TELECALLER", "ACCOUNTANT"}


def test_delivery_read_holders():
    for role in DELIVERY_READ_HOLDERS:
        assert has_permission(role, "delivery:read"), f"{role} should hold delivery:read"


def test_delivery_write_holders():
    for role in DELIVERY_WRITE_HOLDERS:
        assert has_permission(role, "delivery:write"), f"{role} should hold delivery:write"
    for role in DELIVERY_WRITE_NON_HOLDERS:
        assert not has_permission(role, "delivery:write"), \
            f"{role} unexpectedly holds delivery:write (read-only delivery viewer)"


def test_handovers_read_holders():
    for role in HANDOVERS_HOLDERS:
        assert has_permission(role, "handovers:read"), f"{role} should hold handovers:read"


def test_handovers_write_holders():
    for role in HANDOVERS_HOLDERS:
        assert has_permission(role, "handovers:write"), f"{role} should hold handovers:write"


def test_notifications_write_holders():
    for role in NOTIFICATIONS_WRITE_HOLDERS:
        assert has_permission(role, "notifications:write"), f"{role} should hold notifications:write"
    for role in NOTIFICATIONS_WRITE_NON_HOLDERS:
        assert not has_permission(role, "notifications:write"), \
            f"{role} unexpectedly holds notifications:write (admin-tier only)"


def test_finance_read_holders():
    for role in FINANCE_READ_HOLDERS:
        assert has_permission(role, "finance:read"), f"{role} should hold finance:read"
    for role in FINANCE_READ_NON_HOLDERS:
        assert not has_permission(role, "finance:read"), \
            f"{role} unexpectedly holds finance:read (loses the cost/profit reports)"


def test_reports_read_operational_no_regression():
    for role in REPORTS_READ_OPERATIONAL_VIEWERS:
        assert has_permission(role, "reports:read"), f"{role} lost reports:read"
    for role in REPORTS_READ_INTENTIONAL_DROPS:
        assert not has_permission(role, "reports:read"), \
            f"{role} unexpectedly holds reports:read (intended drop from order-performance)"


def test_users_read_no_regression():
    for role in USERS_READ_KEEPERS:
        assert has_permission(role, "users:read"), f"{role} lost users:read"
    for role in USERS_READ_INTENTIONAL_DROPS:
        assert not has_permission(role, "users:read"), \
            f"{role} unexpectedly holds users:read (intended narrow)"


def test_inventory_audits_reuse_outlet_manager():
    # inventory_audits.py reuses inventory perms: OUTLET_MANAGER lists (inventory:read) and
    # submits (inventory:write), but admin audit ops (generate/summary) are inventory:adjust only.
    assert has_permission("OUTLET_MANAGER", "inventory:read"), "OUTLET_MANAGER should hold inventory:read"
    assert has_permission("OUTLET_MANAGER", "inventory:write"), "OUTLET_MANAGER should hold inventory:write"
    assert not has_permission("OUTLET_MANAGER", "inventory:adjust"), \
        "OUTLET_MANAGER unexpectedly holds inventory:adjust (admin audit ops only)"


# --- batch 4d-4 (CRM dev-only) ----------------------------------------------
# leads.py/facebook.py are CRM/Facebook = DEV-ONLY (kept OUT of dev->prod merges).
# leads reads/writes were gated by the legacy CRM role-lists; now `leads:read`/`leads:write`.
# Admin lead ops (import/distribute/delete) -> `leads:manage` (ADMIN+SUPER). All 12 facebook.py
# admin endpoints (pages/mappings/forms/translations) -> `facebook:admin` (ADMIN+SUPER); the Meta
# webhooks stay unauthenticated. leads:read broadens to MARKETING_EXECUTIVE/CEO/CGO/AGENCY_TELECALLER
# (row-scoped to owner_id=self for non-globals; CEO/CGO are GLOBAL -> see all, read-only).
LEADS_READ_LEGACY = {"TELECALLER", "ADMIN", "SUPER_ADMIN"}
LEADS_WRITE_LEGACY = {"TELECALLER", "ADMIN", "SUPER_ADMIN"}
LEADS_MANAGE_HOLDERS = {"ADMIN", "SUPER_ADMIN"}
LEADS_MANAGE_NON_HOLDERS = {"TELECALLER", "OUTLET_MANAGER"}
FACEBOOK_ADMIN_HOLDERS = {"ADMIN", "SUPER_ADMIN"}
FACEBOOK_ADMIN_NON_HOLDERS = {"TELECALLER", "OUTLET_MANAGER", "MARKETING_EXECUTIVE"}
# Roles newly reaching the leads reads (row-scoped, or global for leadership).
LEADS_READ_BROADENED = {"CEO", "CGO", "MARKETING_EXECUTIVE", "AGENCY_TELECALLER"}


def test_leads_read_no_regression():
    for role in LEADS_READ_LEGACY:
        assert has_permission(role, "leads:read"), f"{role} lost leads:read"


def test_leads_write_no_regression():
    for role in LEADS_WRITE_LEGACY:
        assert has_permission(role, "leads:write"), f"{role} lost leads:write"


def test_leads_manage_is_admin_tier():
    for role in LEADS_MANAGE_HOLDERS:
        assert has_permission(role, "leads:manage"), f"{role} should hold leads:manage"
    for role in LEADS_MANAGE_NON_HOLDERS:
        assert not has_permission(role, "leads:manage"), \
            f"{role} unexpectedly holds leads:manage (admin-tier only)"


def test_facebook_admin_is_admin_tier():
    for role in FACEBOOK_ADMIN_HOLDERS:
        assert has_permission(role, "facebook:admin"), f"{role} should hold facebook:admin"
    for role in FACEBOOK_ADMIN_NON_HOLDERS:
        assert not has_permission(role, "facebook:admin"), \
            f"{role} unexpectedly holds facebook:admin (admin-tier only)"


def test_leads_read_broadened():
    for role in LEADS_READ_BROADENED:
        assert has_permission(role, "leads:read"), f"{role} should hold leads:read"


# --- Step 5 (finance) -------------------------------------------------------
# Finance routers (transactions/payouts/products-cost/driver-pay/collections/invoices/
# finance reads) convert legacy require_roles -> require_permission. §A grants ADMIN the
# full operational finance set, ACCOUNTANT payouts:approve, OUTLET_MANAGER
# transactions:write + payouts:read, and TELECALLER transactions:read/write. These holder
# sets pin §F and only pass once Implementer-1's §A grants land (intended gate).
# SUPER_ADMIN is wildcard throughout.
#
# transactions:write = ACCOUNTANT, FINANCE_ADMIN, ADMIN, OUTLET_MANAGER, TELECALLER (+SUPER).
# transactions:read  = that set + finance oversight (CEO, CFO, AUDITOR).
TRANSACTIONS_WRITE_HOLDERS = {
    "ACCOUNTANT", "FINANCE_ADMIN", "ADMIN", "OUTLET_MANAGER", "TELECALLER", "SUPER_ADMIN",
}
TRANSACTIONS_READ_HOLDERS = TRANSACTIONS_WRITE_HOLDERS | {"CEO", "CFO", "AUDITOR"}
# payouts:read is broad (operational managers + finance oversight); payouts:write/approve are
# the finance/admin checker tier only (OUTLET_MANAGER reads but cannot write/approve).
PAYOUTS_READ_HOLDERS = {
    "ACCOUNTANT", "FINANCE_ADMIN", "ADMIN", "OUTLET_MANAGER",
    "CEO", "CFO", "AUDITOR", "SUPER_ADMIN",
}
PAYOUTS_WRITE_HOLDERS = {"ACCOUNTANT", "FINANCE_ADMIN", "ADMIN", "SUPER_ADMIN"}
PAYOUTS_WRITE_NON_HOLDERS = {"OUTLET_MANAGER", "TELECALLER"}
PAYOUTS_APPROVE_HOLDERS = {"ACCOUNTANT", "FINANCE_ADMIN", "ADMIN", "SUPER_ADMIN"}
PAYOUTS_APPROVE_NON_HOLDERS = {"OUTLET_MANAGER", "WAREHOUSE_MANAGER"}
# products:cost:write is the endpoint-level cost lock on product create/update — finance/admin
# only. WAREHOUSE_MANAGER & CATALOGUE_ADMIN intentionally CANNOT create/update products, but
# keep products:write for deactivate + category create.
PRODUCTS_COST_WRITE_HOLDERS = {"FINANCE_ADMIN", "ADMIN", "SUPER_ADMIN"}
PRODUCTS_COST_WRITE_NON_HOLDERS = {"WAREHOUSE_MANAGER", "CATALOGUE_ADMIN", "OUTLET_MANAGER"}
PRODUCTS_WRITE_HOLDERS = {"WAREHOUSE_MANAGER", "ADMIN", "CATALOGUE_ADMIN", "SUPER_ADMIN"}
# driver_pay:write gates the payout_frequency field-lock on delivery_guys; finance/admin tier.
DRIVER_PAY_WRITE_HOLDERS = {"ACCOUNTANT", "FINANCE_ADMIN", "ADMIN", "SUPER_ADMIN"}
DRIVER_PAY_WRITE_NON_HOLDERS = {"WAREHOUSE_MANAGER"}
# collections:write broadens to OUTLET_MANAGER (records own-outlet deposit) + finance/admin.
COLLECTIONS_WRITE_HOLDERS = {"OUTLET_MANAGER", "ACCOUNTANT", "FINANCE_ADMIN", "ADMIN", "SUPER_ADMIN"}
# invoices:write = OUTLET_MANAGER + finance-admin tier; ACCOUNTANT intentionally EXCLUDED
# (legacy never let accountant create/update invoices).
INVOICES_WRITE_HOLDERS = {"OUTLET_MANAGER", "FINANCE_ADMIN", "ADMIN", "SUPER_ADMIN"}
INVOICES_WRITE_NON_HOLDERS = {"ACCOUNTANT"}
# finance:read no-regression for the gst-summary (finance-only) viewers; OUTLET_MANAGER excluded.
FINANCE_READ_STEP5_HOLDERS = {
    "ACCOUNTANT", "ADMIN", "FINANCE_ADMIN", "AUDITOR", "CEO", "CFO", "SUPER_ADMIN",
}
FINANCE_READ_STEP5_NON_HOLDERS = {"OUTLET_MANAGER"}


def test_transactions_write_holders():
    for role in TRANSACTIONS_WRITE_HOLDERS:
        assert has_permission(role, "transactions:write"), f"{role} should hold transactions:write"


def test_transactions_read_holders():
    for role in TRANSACTIONS_READ_HOLDERS:
        assert has_permission(role, "transactions:read"), f"{role} should hold transactions:read"


def test_payouts_read_holders():
    for role in PAYOUTS_READ_HOLDERS:
        assert has_permission(role, "payouts:read"), f"{role} should hold payouts:read"


def test_payouts_write_holders():
    for role in PAYOUTS_WRITE_HOLDERS:
        assert has_permission(role, "payouts:write"), f"{role} should hold payouts:write"
    for role in PAYOUTS_WRITE_NON_HOLDERS:
        assert not has_permission(role, "payouts:write"), \
            f"{role} unexpectedly holds payouts:write (finance/admin checker tier only)"


def test_payouts_approve_holders():
    for role in PAYOUTS_APPROVE_HOLDERS:
        assert has_permission(role, "payouts:approve"), f"{role} should hold payouts:approve"
    for role in PAYOUTS_APPROVE_NON_HOLDERS:
        assert not has_permission(role, "payouts:approve"), \
            f"{role} unexpectedly holds payouts:approve (finance/admin checker tier only)"


def test_products_cost_write_holders():
    for role in PRODUCTS_COST_WRITE_HOLDERS:
        assert has_permission(role, "products:cost:write"), f"{role} should hold products:cost:write"
    for role in PRODUCTS_COST_WRITE_NON_HOLDERS:
        assert not has_permission(role, "products:cost:write"), \
            f"{role} unexpectedly holds products:cost:write (cost-write lock — finance/admin only)"


def test_products_write_no_regression():
    # deactivate + category create stay on products:write (warehouse/catalogue keep these).
    for role in PRODUCTS_WRITE_HOLDERS:
        assert has_permission(role, "products:write"), f"{role} lost products:write"


def test_driver_pay_write_holders():
    for role in DRIVER_PAY_WRITE_HOLDERS:
        assert has_permission(role, "driver_pay:write"), f"{role} should hold driver_pay:write"
    for role in DRIVER_PAY_WRITE_NON_HOLDERS:
        assert not has_permission(role, "driver_pay:write"), \
            f"{role} unexpectedly holds driver_pay:write (payout_frequency field-lock)"


def test_collections_write_holders():
    for role in COLLECTIONS_WRITE_HOLDERS:
        assert has_permission(role, "collections:write"), f"{role} should hold collections:write"


def test_invoices_write_holders():
    for role in INVOICES_WRITE_HOLDERS:
        assert has_permission(role, "invoices:write"), f"{role} should hold invoices:write"
    for role in INVOICES_WRITE_NON_HOLDERS:
        assert not has_permission(role, "invoices:write"), \
            f"{role} unexpectedly holds invoices:write (legacy excluded accountant from invoice create/update)"


def test_finance_read_step5_gst_summary_no_regression():
    for role in FINANCE_READ_STEP5_HOLDERS:
        assert has_permission(role, "finance:read"), f"{role} lost finance:read (gst-summary viewer)"
    for role in FINANCE_READ_STEP5_NON_HOLDERS:
        assert not has_permission(role, "finance:read"), \
            f"{role} unexpectedly holds finance:read (excluded from gst-summary)"


if __name__ == "__main__":
    test_inventory_read_no_regression()
    test_inventory_write_no_regression()
    test_inventory_write_intentional_drops()
    test_inventory_adjust_is_admin_tier()
    test_warehouse_is_global_for_all_outlet_visibility()
    test_transfers_read_no_regression()
    test_transfers_write_no_regression()
    test_transfers_read_broadened()
    test_transfers_write_broadened()
    test_orders_read_no_regression()
    test_orders_write_no_regression()
    test_orders_status_holders()
    test_orders_manage_is_admin_tier()
    test_orders_revoke_is_super_only()
    test_orders_read_broadened()
    test_outlets_read_no_regression()
    test_outlets_write_holders()
    test_clusters_write_holders()
    test_clusters_read_no_regression()
    test_config_read_holders()
    test_config_write_holders()
    test_audit_read_holders()
    test_delivery_read_holders()
    test_delivery_write_holders()
    test_handovers_read_holders()
    test_handovers_write_holders()
    test_notifications_write_holders()
    test_finance_read_holders()
    test_reports_read_operational_no_regression()
    test_users_read_no_regression()
    test_inventory_audits_reuse_outlet_manager()
    test_leads_read_no_regression()
    test_leads_write_no_regression()
    test_leads_manage_is_admin_tier()
    test_facebook_admin_is_admin_tier()
    test_leads_read_broadened()
    test_transactions_write_holders()
    test_transactions_read_holders()
    test_payouts_read_holders()
    test_payouts_write_holders()
    test_payouts_approve_holders()
    test_products_cost_write_holders()
    test_products_write_no_regression()
    test_driver_pay_write_holders()
    test_collections_write_holders()
    test_invoices_write_holders()
    test_finance_read_step5_gst_summary_no_regression()
    print("OK")
