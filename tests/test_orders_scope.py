"""Row-scope logic for orders.py (Step 4c), driven without a live DB.

Orders are scoped by FUNCTION, not by one geography column: telecallers see only
the orders they created, delivery riders only the ones assigned to them, geographic
managers by the order's assigned outlet, and agency admins by their agency's
telecallers. These checks pin that branching by faking the scope-expansion helpers:

  * `_apply_order_scope` -> filters unchanged for GLOBAL/microservice; adds
    delivery_person_id for riders, telecaller_id for own-created roles, and (via
    apply_scope) assigned_outlet_id for OUTLET/CLUSTER managers / telecaller_id for AGENCY.
  * `_assert_order_in_scope` -> no-op for GLOBAL, passes when the order matches the
    caller's function/scope, raises 403 otherwise.

`import path_setup` wires SharedBackend (mirrors tests/test_transfers_scope.py);
manager/auth imports stay inside the tests so collection doesn't build the managers
tree eagerly. The helpers call utils.auth.apply_scope, which references auth's module
globals `_scoped_outlet_ids` / `_scoped_telecaller_ids`; the CLUSTER/AGENCY cases
monkeypatch those (and restore in finally) so no DB is touched. The OUTLET branch is
DB-free already. Run::

    python tests/test_orders_scope.py     # or: pytest tests/test_orders_scope.py
"""
import os
import sys
import asyncio
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:  # path_setup prints a ✅ banner; keep it from blowing up a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports


def _order(telecaller_id=None, delivery_person_id=None, assigned_outlet_id=None, agency_id=None):
    return SimpleNamespace(
        telecaller_id=telecaller_id,
        delivery_person_id=delivery_person_id,
        assigned_outlet_id=assigned_outlet_id,
        agency_id=agency_id,
    )


def test_apply_order_scope_global_unchanged():
    import routers.v1.orders as O
    from utils.auth import AuthContext

    g = AuthContext(user_id="u", role="ADMIN", scope_level="GLOBAL")
    out = asyncio.run(O._apply_order_scope({"status": "PENDING"}, g))
    assert out == {"status": "PENDING"}                  # GLOBAL = no narrowing


def test_apply_order_scope_delivery():
    import routers.v1.orders as O
    from utils.auth import AuthContext

    d = AuthContext(user_id="u", role="DELIVERY_GUY", scope_level="OUTLET")
    out = asyncio.run(O._apply_order_scope({}, d))
    assert out == {"delivery_person_id": "u"}             # rider: only own deliveries


def test_apply_order_scope_own_created():
    import routers.v1.orders as O
    from utils.auth import AuthContext

    t = AuthContext(user_id="u", role="TELECALLER", scope_level="OUTLET")
    out = asyncio.run(O._apply_order_scope({}, t))
    assert out == {"telecaller_id": "u"}                  # telecaller: only own-created


def test_apply_order_scope_outlet_manager():
    import routers.v1.orders as O
    from utils.auth import AuthContext

    m = AuthContext(user_id="u", role="OUTLET_MANAGER", scope_level="OUTLET", outlet_id="O")
    out = asyncio.run(O._apply_order_scope({}, m))
    assert out == {"assigned_outlet_id": "O"}             # OUTLET branch is DB-free


def test_apply_order_scope_cluster():
    import routers.v1.orders as O
    from utils.auth import AuthContext
    from utils import auth

    orig = auth._scoped_outlet_ids
    async def _ids(ctx):
        return ["o1", "o2"]
    auth._scoped_outlet_ids = _ids
    try:
        c = AuthContext(user_id="u", role="CLUSTER_MANAGER", scope_level="CLUSTER", cluster_ids=["c1"])
        out = asyncio.run(O._apply_order_scope({}, c))
        assert out == {"assigned_outlet_id": ["o1", "o2"]}   # cluster -> covered outlets
    finally:
        auth._scoped_outlet_ids = orig


def test_apply_order_scope_agency():
    import routers.v1.orders as O
    from utils.auth import AuthContext
    from utils import auth

    orig = auth._scoped_telecaller_ids
    async def _ids(ctx):
        return ["t1", "t2"]
    auth._scoped_telecaller_ids = _ids
    try:
        a = AuthContext(user_id="u", role="AGENCY_ADMIN", scope_level="AGENCY", agency_ids=["a1"])
        out = asyncio.run(O._apply_order_scope({}, a))
        assert out == {"telecaller_id": ["t1", "t2"]}        # agency -> its telecallers
    finally:
        auth._scoped_telecaller_ids = orig


def test_assert_order_in_scope_global():
    import routers.v1.orders as O
    from utils.auth import AuthContext

    g = AuthContext(user_id="u", role="ADMIN", scope_level="GLOBAL")
    # GLOBAL is never fenced, whatever the order looks like.
    asyncio.run(O._assert_order_in_scope(g, _order(telecaller_id="other", assigned_outlet_id="X")))


def test_assert_order_in_scope_telecaller():
    import routers.v1.orders as O
    from utils.auth import AuthContext
    from fastapi import HTTPException

    t = AuthContext(user_id="u", role="TELECALLER", scope_level="OUTLET")
    asyncio.run(O._assert_order_in_scope(t, _order(telecaller_id="u")))      # own order ok
    try:
        asyncio.run(O._assert_order_in_scope(t, _order(telecaller_id="other")))
        assert False, "expected a 403 for another telecaller's order"
    except HTTPException as e:
        assert e.status_code == 403


def test_assert_order_in_scope_delivery():
    import routers.v1.orders as O
    from utils.auth import AuthContext
    from fastapi import HTTPException

    d = AuthContext(user_id="u", role="DELIVERY_GUY", scope_level="OUTLET")
    asyncio.run(O._assert_order_in_scope(d, _order(delivery_person_id="u")))  # own delivery ok
    try:
        asyncio.run(O._assert_order_in_scope(d, _order(delivery_person_id="other")))
        assert False, "expected a 403 for another rider's delivery"
    except HTTPException as e:
        assert e.status_code == 403


def test_assert_order_in_scope_outlet_manager():
    import routers.v1.orders as O
    from utils.auth import AuthContext
    from fastapi import HTTPException

    m = AuthContext(user_id="u", role="OUTLET_MANAGER", scope_level="OUTLET", outlet_id="O")
    asyncio.run(O._assert_order_in_scope(m, _order(assigned_outlet_id="O")))   # own outlet ok
    try:
        asyncio.run(O._assert_order_in_scope(m, _order(assigned_outlet_id="X")))
        assert False, "expected a 403 for an order outside the manager's outlet"
    except HTTPException as e:
        assert e.status_code == 403


def test_assert_order_in_scope_agency():
    import routers.v1.orders as O
    from utils.auth import AuthContext
    from fastapi import HTTPException

    a = AuthContext(user_id="u", role="AGENCY_ADMIN", scope_level="AGENCY", agency_ids=["a1"])
    asyncio.run(O._assert_order_in_scope(a, _order(agency_id="a1")))           # own agency ok
    try:
        asyncio.run(O._assert_order_in_scope(a, _order(agency_id="a2")))
        assert False, "expected a 403 for an order outside the admin's agencies"
    except HTTPException as e:
        assert e.status_code == 403


if __name__ == "__main__":
    test_apply_order_scope_global_unchanged()
    test_apply_order_scope_delivery()
    test_apply_order_scope_own_created()
    test_apply_order_scope_outlet_manager()
    test_apply_order_scope_cluster()
    test_apply_order_scope_agency()
    test_assert_order_in_scope_global()
    test_assert_order_in_scope_telecaller()
    test_assert_order_in_scope_delivery()
    test_assert_order_in_scope_outlet_manager()
    test_assert_order_in_scope_agency()
    print("OK")
