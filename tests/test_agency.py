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


# --- create_user (Task 8: SUPER_ADMIN creating TELECALLER / AGENCY_TELECALLER) ---
#
# This codebase's test suite is deliberately DB-free (no conftest.py, no
# TestClient(app) fixture — tests/test_admin.py's `from app import app` even
# fails to collect here: `ModuleNotFoundError: path_setup`). Rather than stand
# up a real DB, these tests call the router's create_user() coroutine
# directly with a monkeypatched user_manager (fetch_all/create stubbed with
# async fakes) — the same function FastAPI invokes for `POST /users`, so a
# clean return (no HTTPException) is exactly what a 201 requires.

def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _create_user_as_super_admin(payload_kwargs, monkeypatch):
    from utils.auth import AuthContext
    from models import UserCreateRequest
    from routers.v1 import users as users_router

    class _FakeFetchAllResult:
        items = []

    async def _fake_fetch_all(*a, **k):
        return _FakeFetchAllResult()

    async def _fake_create(user):
        # Mimic the DB assigning identity/timestamps on insert.
        import datetime
        user.uid = "new-user-uid"
        user.created_at = datetime.datetime.now(datetime.timezone.utc)
        return user

    monkeypatch.setattr(users_router.user_manager, "fetch_all", _fake_fetch_all)
    monkeypatch.setattr(users_router.user_manager, "create", _fake_create)

    ctx = AuthContext(user_id="s", role="SUPER_ADMIN", scope_level="GLOBAL", perms={"*"})
    payload = UserCreateRequest(**payload_kwargs)
    return _run(users_router.create_user(payload, ctx))


def test_create_user_agency_telecaller_with_agency_returns_201_body(monkeypatch):
    response = _create_user_as_super_admin(dict(
        email="agency.tc@example.com",
        password="password123",
        full_name="Agency TC",
        role="AGENCY_TELECALLER",
        phone="9876543210",
        agency_id="ag1",
        assignment_quota=25,
    ), monkeypatch)
    assert response.role == "AGENCY_TELECALLER"
    assert response.agency_id == "ag1"
    assert response.assignment_quota == 25  # was silently dropped before the Task 8 fix


def test_create_user_telecaller_without_agency_returns_201_body(monkeypatch):
    response = _create_user_as_super_admin(dict(
        email="telecaller@example.com",
        password="password123",
        full_name="Plain TC",
        role="TELECALLER",
        phone="9876543211",
    ), monkeypatch)
    assert response.role == "TELECALLER"
    assert response.agency_id is None


def test_report_scope_owner(monkeypatch):
    """Prospect-report scoping: global → None (all leads), agency admin → their
    roster's owner ids (deny-by-default when empty), other scoped → own uid."""
    from utils import auth as A
    from routers.v1 import leads as leads_router

    async def _fake_ids(ctx):
        return ["t1", "t2"]
    monkeypatch.setattr(A, "_scoped_telecaller_ids", _fake_ids)

    sa = A.AuthContext(user_id="s", role="SUPER_ADMIN", scope_level="GLOBAL", perms={"*"})
    assert _run(leads_router._report_scope_owner(sa)) is None
    ad = A.AuthContext(user_id="ad", role="AGENCY_ADMIN", scope_level="AGENCY", agency_ids=["ag1"])
    assert _run(leads_router._report_scope_owner(ad)) == ["t1", "t2"]
    tc = A.AuthContext(user_id="tc1", role="TELECALLER", scope_level="OUTLET")
    assert _run(leads_router._report_scope_owner(tc)) == "tc1"

    async def _no_ids(ctx):
        return []
    monkeypatch.setattr(A, "_scoped_telecaller_ids", _no_ids)
    ad0 = A.AuthContext(user_id="ad", role="AGENCY_ADMIN", scope_level="AGENCY", agency_ids=[])
    assert _run(leads_router._report_scope_owner(ad0)) == ["__none__"]


class _MinimalMonkeypatch:
    """Tiny stand-in for pytest's `monkeypatch` fixture, for the __main__ runner
    below (pytest itself injects the real fixture when run via `pytest`)."""
    def __init__(self):
        self._restores = []

    def setattr(self, obj, name, value):
        self._restores.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._restores):
            setattr(obj, name, old)


if __name__ == "__main__":
    import inspect
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            mp = _MinimalMonkeypatch() if "monkeypatch" in inspect.signature(f).parameters else None
            try:
                f(mp) if mp else f()
                print("PASS", n)
            except AssertionError as e:
                fails += 1; print("FAIL", n, e)
            finally:
                if mp:
                    mp.undo()
    print("OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
