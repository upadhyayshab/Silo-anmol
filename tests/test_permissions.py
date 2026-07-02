"""
Unit tests for the RBAC permission registry (utils/permissions.py).

Pure / DB-free: no FastAPI, no DB, no settings. Run with:
    python -m pytest tests/test_permissions.py
or directly:
    python tests/test_permissions.py
"""
import os
import sys

# The app runs with app/ as the import root (e.g. `from utils.constants import`).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from utils.constants import UserRole
from utils.permissions import (
    Permission, ScopeLevel, WILDCARD, ROLE_DEFINITIONS,
    has_permission, masked_columns_for, role_scope_level, role_perms, set_role_cache,
)


def test_every_userrole_has_a_definition():
    missing = [r.value for r in UserRole if r not in ROLE_DEFINITIONS]
    assert not missing, f"roles without a ROLE_DEFINITIONS entry: {missing}"


def test_super_admin_is_wildcard():
    assert WILDCARD in role_perms(UserRole.SUPER_ADMIN)
    # wildcard passes any permission check, including ones it never enumerates
    assert has_permission(UserRole.SUPER_ADMIN, Permission.PAYOUTS_APPROVE)
    assert has_permission("SUPER_ADMIN", Permission.CONFIG_WRITE)  # string role too


def test_cost_columns_masked_below_finance():
    # Roles WITHOUT products:cost:read get cost/margin/commission stripped.
    for role in (UserRole.TELECALLER, UserRole.OUTLET_MANAGER, UserRole.WAREHOUSE_MANAGER):
        assert masked_columns_for("products", role) == ["cost_price", "margin", "commission"]
    # Finance / leadership WITH products:cost:read see them.
    for role in (UserRole.ACCOUNTANT, UserRole.CFO, UserRole.SUPER_ADMIN, UserRole.AUDITOR):
        assert masked_columns_for("products", role) == []


def test_orders_commission_masked_below_finance():
    # total_commission on orders is gated by products:cost:read (same finance veil).
    # Roles WITHOUT it get total_commission nulled.
    for role in (UserRole.TELECALLER, UserRole.OUTLET_MANAGER):
        assert masked_columns_for("orders", role) == ["total_commission"]
    # Finance (products:cost:read) and SUPER_ADMIN (wildcard) see it.
    for role in (UserRole.ACCOUNTANT, UserRole.SUPER_ADMIN):
        assert masked_columns_for("orders", role) == []


def test_cgo_sees_topline_not_margin_or_pay():
    # Growth head: catalogue + orders + campaigns, but NOT cost/margin or payouts.
    assert has_permission(UserRole.CGO, Permission.PRODUCTS_READ)
    assert has_permission(UserRole.CGO, Permission.CAMPAIGNS_READ)
    assert not has_permission(UserRole.CGO, Permission.PRODUCTS_COST_READ)
    assert not has_permission(UserRole.CGO, Permission.PAYOUTS_READ)
    assert masked_columns_for("products", UserRole.CGO) == ["cost_price", "margin", "commission"]


def test_gstin_pan_masked_below_l3():
    # Telecaller/delivery (L1-L2) don't get outlet tax ids.
    assert masked_columns_for("outlets", UserRole.TELECALLER) == ["gstin", "pan"]
    assert masked_columns_for("outlets", UserRole.DELIVERY_GUY) == ["gstin", "pan"]
    # Managers (L3+) do.
    assert masked_columns_for("outlets", UserRole.OUTLET_MANAGER) == []
    assert masked_columns_for("outlets", UserRole.STATE_HEAD) == []


def test_leadership_is_read_only_but_l4_finance_writes():
    # Leadership personas hold no write/approve permissions.
    for role in (UserRole.CEO, UserRole.CFO, UserRole.COO, UserRole.CGO):
        granted = role_perms(role)
        writes = [p for p in granted if p.endswith(":write") or p.endswith(":approve")]
        assert not writes, f"leadership {role.value} unexpectedly has writes: {writes}"
    # Accountant (L4 finance) writes ops finance AND driver salary structure.
    assert has_permission(UserRole.ACCOUNTANT, Permission.COLLECTIONS_WRITE)
    assert has_permission(UserRole.ACCOUNTANT, Permission.PAYOUTS_WRITE)
    assert has_permission(UserRole.ACCOUNTANT, Permission.DRIVER_PAY_WRITE)
    # ...but driver salary structure is NOT an outlet manager's to set.
    assert not has_permission(UserRole.OUTLET_MANAGER, Permission.DRIVER_PAY_WRITE)


def test_cost_edit_is_finance_only():
    # Catalogue admin edits the catalogue but NOT cost/margin.
    assert has_permission(UserRole.CATALOGUE_ADMIN, Permission.PRODUCTS_WRITE)
    assert not has_permission(UserRole.CATALOGUE_ADMIN, Permission.PRODUCTS_COST_WRITE)
    # Finance admin / super admin can.
    assert has_permission(UserRole.FINANCE_ADMIN, Permission.PRODUCTS_COST_WRITE)
    assert has_permission(UserRole.SUPER_ADMIN, Permission.PRODUCTS_COST_WRITE)


def test_payouts_approve_is_finance_admin_tier():
    # Step 5 (user decision 2026-06-29): NO maker-checker separation — ACCOUNTANT keeps
    # both write AND approve (pure permission gating, not segregation of duties).
    # payouts:approve is the finance/admin tier; ops roles never hold it.
    for r in (UserRole.ACCOUNTANT, UserRole.FINANCE_ADMIN, UserRole.ADMIN, UserRole.SUPER_ADMIN):
        assert has_permission(r, Permission.PAYOUTS_APPROVE), r
    assert has_permission(UserRole.ACCOUNTANT, Permission.PAYOUTS_WRITE)
    for r in (UserRole.OUTLET_MANAGER, UserRole.WAREHOUSE_MANAGER, UserRole.TELECALLER):
        assert not has_permission(r, Permission.PAYOUTS_APPROVE), r


def test_roles_manage_is_super_admin_only():
    # roles:manage gates the roles admin API. It's not granted explicitly to any role —
    # SUPER_ADMIN gets it only via WILDCARD. Exercise the code-fallback path (empty cache).
    set_role_cache({})
    for role in UserRole:
        expected = (role == UserRole.SUPER_ADMIN)
        assert has_permission(role, Permission.ROLES_MANAGE) == expected, role.value


def test_scope_levels():
    assert role_scope_level(UserRole.OUTLET_MANAGER) == ScopeLevel.OUTLET
    assert role_scope_level(UserRole.CLUSTER_MANAGER) == ScopeLevel.CLUSTER
    assert role_scope_level(UserRole.STATE_HEAD) == ScopeLevel.STATE
    assert role_scope_level(UserRole.CEO) == ScopeLevel.GLOBAL


# --------------------------------------------------------------------------
# Task 7: users list/get/create responses expose full user detail (state,
# activity timestamps) so the admin Users table can render them. DB-free
# checks only (this module stays config-free); see routers/v1/users.py.
# --------------------------------------------------------------------------

def test_user_response_has_full_detail_fields():
    from models import UserResponse
    for field in ("state", "created_at", "updated_at", "last_active_at",
                  "agency_id", "assignment_quota"):
        assert field in UserResponse.model_fields, f"UserResponse missing {field}"


def test_user_schema_has_full_detail_columns():
    # Guards the DB columns the router reads from stay in sync with UserResponse.
    # managers.py needs SharedBackend on sys.path first (see app/path_setup.py);
    # scoped to this test so the module stays DB-free for everything else.
    import path_setup  # noqa: F401
    from managers import UserSchema
    for column in ("state", "created_at", "updated_at", "last_active_at",
                   "agency_id", "assignment_quota"):
        assert hasattr(UserSchema, column), f"UserSchema missing {column}"


def test_users_router_populates_full_detail_on_list_get_create_update():
    # Source-level regression guard: every UserResponse(...) construction site in
    # the users router must pass state/last_active_at (the fields that were being
    # silently dropped before Task 7), not just declare them on the model.
    import re
    users_router_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "routers", "v1", "users.py")
    with open(users_router_path, encoding="utf-8") as f:
        src = f.read()

    calls = re.findall(r"UserResponse\((?:[^()]|\([^()]*\))*\)", src)
    assert len(calls) >= 4, f"expected list/get/create/update UserResponse(...) sites, found {len(calls)}"
    for call in calls:
        assert "state=" in call, f"UserResponse(...) missing state=: {call[:80]}..."
        assert "last_active_at=" in call, f"UserResponse(...) missing last_active_at=: {call[:80]}..."


def _check_apply_scope():
    """apply_scope branching, override, and deny-by-default. Needs utils.auth
    (-> config); run only from __main__ so pytest collection stays config-free."""
    import asyncio
    from utils import auth as A

    async def run():
        A._scoped_outlet_ids = lambda ctx: _aval(["o1", "o2"])
        g = A.AuthContext(user_id="a", role="SUPER_ADMIN", scope_level="GLOBAL", perms={"*"})
        assert await A.apply_scope({"x": 1}, g) == {"x": 1}                       # global no-op
        o = A.AuthContext(user_id="b", role="OUTLET_MANAGER", scope_level="OUTLET", outlet_id="O9")
        assert await A.apply_scope({}, o) == {"outlet_id": "O9"}                  # own outlet
        assert await A.apply_scope({"outlet_id": "OTHER"}, o) == {"outlet_id": "O9"}  # can't escape
        c = A.AuthContext(user_id="c", role="CLUSTER_MANAGER", scope_level="CLUSTER", cluster_ids=["c1"])
        assert await A.apply_scope({}, c) == {"outlet_id": ["o1", "o2"]}          # cluster expand
        A._scoped_outlet_ids = lambda ctx: _aval([])
        c0 = A.AuthContext(user_id="d", role="CLUSTER_MANAGER", scope_level="CLUSTER", cluster_ids=[])
        assert await A.apply_scope({}, c0) == {"outlet_id": ["__none__"]}         # deny-by-default

    async def _aval(v):
        return v

    asyncio.run(run())


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    try:
        _check_apply_scope()
        print("PASS apply_scope (global/outlet/override/cluster/deny)")
    except AssertionError as e:
        failures += 1
        print(f"FAIL apply_scope: {e}")
    except Exception as e:
        print(f"SKIP apply_scope (needs config): {type(e).__name__}: {e}")
    print(f"\n{'OK' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
