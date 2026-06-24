import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import path_setup  # noqa: F401 — must be first; wires SharedBackend onto sys.path


def test_agency_schema_columns():
    from managers import AgencySchema, AgencyManager, UserSchema, CustomerOrderSchema
    assert AgencySchema.__tablename__ == "agencies"
    for c in ("uid", "name", "is_active"):
        assert hasattr(AgencySchema, c), f"AgencySchema missing {c}"
    assert hasattr(UserSchema, "agency_id"), "users.agency_id missing"
    assert hasattr(CustomerOrderSchema, "agency_id"), "customer_orders.agency_id missing"


def test_agency_roles():
    from utils.constants import UserRole
    from utils.permissions import (Permission as P, ScopeLevel, has_permission,
                                    role_scope_level)
    assert has_permission(UserRole.AGENCY_TELECALLER, P.ORDERS_WRITE)
    assert has_permission(UserRole.AGENCY_TELECALLER, P.LEADS_READ)
    assert not has_permission(UserRole.AGENCY_TELECALLER, P.FINANCE_READ)
    assert not has_permission(UserRole.AGENCY_TELECALLER, P.PRODUCTS_COST_READ)
    assert has_permission(UserRole.AGENCY_ADMIN, P.USERS_MANAGE)
    assert has_permission(UserRole.AGENCY_ADMIN, P.ORDERS_READ)
    assert not has_permission(UserRole.AGENCY_ADMIN, P.ORDERS_WRITE)
    assert not has_permission(UserRole.AGENCY_ADMIN, P.FINANCE_READ)
    assert role_scope_level(UserRole.AGENCY_ADMIN) == ScopeLevel.AGENCY
    assert has_permission(UserRole.ADMIN, P.USERS_MANAGE)


def test_apply_scope_agency():
    import asyncio
    from utils import auth as A

    async def _aval(v):
        return v

    async def run():
        A._scoped_telecaller_ids = lambda ctx: _aval(["t1", "t2"])
        ad = A.AuthContext(user_id="ad", role="AGENCY_ADMIN", scope_level="AGENCY", agency_ids=["ag1"])
        assert await A.apply_scope({}, ad) == {"telecaller_id": ["t1", "t2"]}
        assert await A.apply_scope({"telecaller_id": "OTHER"}, ad) == {"telecaller_id": ["t1", "t2"]}
        A._scoped_telecaller_ids = lambda ctx: _aval([])
        ad0 = A.AuthContext(user_id="ad", role="AGENCY_ADMIN", scope_level="AGENCY", agency_ids=[])
        assert await A.apply_scope({}, ad0) == {"telecaller_id": ["__none__"]}

    asyncio.run(run())


def test_order_response_has_agency_id():
    from models import OrderResponse
    assert "agency_id" in OrderResponse.model_fields, "OrderResponse missing agency_id"


def test_roster_fence():
    from fastapi import HTTPException
    from utils.auth import AuthContext, enforce_agency_roster_fence as fence
    admin = AuthContext(user_id="ad", role="AGENCY_ADMIN", scope_level="AGENCY", agency_ids=["ag1"])
    fence(admin, "AGENCY_TELECALLER", "ag1")  # OK
    try:
        fence(admin, "OUTLET_MANAGER", "ag1"); assert False, "should 403"
    except HTTPException as e:
        assert e.status_code == 403
    try:
        fence(admin, "AGENCY_TELECALLER", "ag2"); assert False, "should 403"
    except HTTPException as e:
        assert e.status_code == 403
    sa = AuthContext(user_id="s", role="SUPER_ADMIN", scope_level="GLOBAL", perms={"*"})
    fence(sa, "ACCOUNTANT", None)  # non-agency-admin: no-op


def test_agency_models_exist():
    from models import AgencyCreateRequest, AgencyResponse
    assert "name" in AgencyCreateRequest.model_fields
    assert "is_active" in AgencyResponse.model_fields


def test_agency_update_fence_blocks_move():
    from fastapi import HTTPException
    from utils.auth import AuthContext, enforce_agency_update_fence as ufence
    admin = AuthContext(user_id="ad", role="AGENCY_ADMIN", scope_level="AGENCY", agency_ids=["ag1"])
    ufence(admin, "AGENCY_TELECALLER", "ag1", {"full_name": "x"})  # OK
    for bad in ({"agency_id": "ag2"}, {"outlet_id": "o9"}):
        try:
            ufence(admin, "AGENCY_TELECALLER", "ag1", bad); assert False, "should 403"
        except HTTPException as e:
            assert e.status_code == 403
    # super admin: no-op
    sa = AuthContext(user_id="s", role="SUPER_ADMIN", scope_level="GLOBAL", perms={"*"})
    ufence(sa, "ACCOUNTANT", None, {"agency_id": "ag2"})


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("PASS", n)
            except AssertionError as e:
                fails += 1; print("FAIL", n, e)
    print("OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
