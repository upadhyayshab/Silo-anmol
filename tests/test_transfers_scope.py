"""Row-scope logic for transfers.py (Step 4b), driven without a live DB.

Transfers carry two outlet FKs (from/to), so a scoped caller is matched by an OR over
both. These checks pin that behaviour by faking the manager + the scope-expansion helper:

  * `_transfer_scope_ids` -> None for GLOBAL/microservice, [outlet] for OUTLET,
    expanded list for CLUSTER/STATE, ["__none__"] deny when a scoped user has no outlet.
  * `_assert_transfer_in_scope` -> no-op for GLOBAL, passes when from/to touches the
    caller's scope, raises 403 otherwise.
  * `_fetch_transfers_scoped` -> GLOBAL passes the query straight through; scoped runs two
    queries (incoming `to ∈ scope`, outgoing `from ∈ scope`), deduped + sorted + paginated.

`import path_setup` wires SharedBackend (mirrors tests/test_agency.py); manager/auth
imports stay inside the tests so collection doesn't build the managers tree eagerly.
The fetch-level deny ("__none__" matches no real row) is exercised against real data by
the dev smoke; here it's pinned at the `_transfer_scope_ids` level. (The AGENCY leg of
apply_scope lives in tests/test_agency.py::test_apply_scope_agency.) Run::

    python tests/test_transfers_scope.py     # or: pytest tests/test_transfers_scope.py
"""
import os
import sys
import asyncio
from types import SimpleNamespace
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:  # path_setup prints a ✅ banner; keep it from blowing up a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports


def _r(uid, day, frm, to):
    return SimpleNamespace(uid=uid, created_at=datetime(2024, 1, day),
                           from_outlet_id=frm, to_outlet_id=to)


class _Res:
    def __init__(self, items):
        self.items = items
        self.count = len(items)


class _FakeMgr:
    """Records fetch_all calls and returns rows chosen by a per-test router fn."""
    def __init__(self, router):
        self.calls = []
        self._router = router

    async def fetch_all(self, *, filters=None, limit=None, offset=0, sorts=None):
        filters = filters or {}
        self.calls.append(dict(filters))
        return _Res(self._router(filters))


def test_transfer_scope_ids_branches():
    import routers.v1.transfers as T
    from utils.auth import AuthContext
    from utils import auth

    g = AuthContext(user_id="u", role="ADMIN", scope_level="GLOBAL")
    assert asyncio.run(T._transfer_scope_ids(g)) is None                # GLOBAL = unrestricted

    ms = AuthContext(user_id="u", role="svc", is_microservice=True, scope_level="OUTLET")
    assert asyncio.run(T._transfer_scope_ids(ms)) is None               # microservice = unrestricted

    o = AuthContext(user_id="u", role="OUTLET_MANAGER", scope_level="OUTLET", outlet_id="O")
    assert asyncio.run(T._transfer_scope_ids(o)) == ["O"]               # own outlet

    d = AuthContext(user_id="u", role="OUTLET_MANAGER", scope_level="OUTLET", outlet_id=None)
    assert asyncio.run(T._transfer_scope_ids(d)) == ["__none__"]        # deny-by-default

    orig = auth._scoped_outlet_ids
    async def _ids(ctx):
        return ["o1", "o2"]
    auth._scoped_outlet_ids = _ids
    try:
        c = AuthContext(user_id="u", role="CLUSTER_MANAGER", scope_level="CLUSTER", cluster_ids=["c1"])
        assert asyncio.run(T._transfer_scope_ids(c)) == ["o1", "o2"]    # cluster -> covered outlets
    finally:
        auth._scoped_outlet_ids = orig


def test_assert_transfer_in_scope():
    import routers.v1.transfers as T
    from utils.auth import AuthContext
    from fastapi import HTTPException

    g = AuthContext(user_id="u", role="ADMIN", scope_level="GLOBAL")
    asyncio.run(T._assert_transfer_in_scope(g, _r("A", 1, "X", "Y")))   # global: never fenced

    o = AuthContext(user_id="u", role="OUTLET_MANAGER", scope_level="OUTLET", outlet_id="O")
    asyncio.run(T._assert_transfer_in_scope(o, _r("A", 1, "X", "O")))   # touches O as destination
    asyncio.run(T._assert_transfer_in_scope(o, _r("B", 1, "O", "X")))   # touches O as source

    try:
        asyncio.run(T._assert_transfer_in_scope(o, _r("C", 1, "X", "Y")))
        assert False, "expected a 403 for an out-of-scope transfer"
    except HTTPException as e:
        assert e.status_code == 403


def test_global_fetch_passes_filters_straight_through():
    import routers.v1.transfers as T
    from utils.auth import AuthContext

    rows = [_r("A", 3, "X", "Y"), _r("B", 2, "Y", "Z")]
    fake = _FakeMgr(lambda f: rows)
    orig = T.transfer_manager
    T.transfer_manager = fake
    try:
        g = AuthContext(user_id="u", role="ADMIN", scope_level="GLOBAL")
        items, total = asyncio.run(T._fetch_transfers_scoped(
            g, {"status": "PENDING"}, from_outlet_id="X", to_outlet_id=None, limit=50, offset=0))
        assert [i.uid for i in items] == ["A", "B"]
        assert total == 2
        assert len(fake.calls) == 1                       # one straight query, no OR fan-out
        assert fake.calls[0]["status"] == "PENDING"
        assert fake.calls[0]["from_outlet_id"] == "X"     # explicit filter honoured
        assert "to_outlet_id" not in fake.calls[0]
    finally:
        T.transfer_manager = orig


def test_scoped_fetch_or_dedup_sort_paginate():
    import routers.v1.transfers as T
    from utils.auth import AuthContext

    # D appears in BOTH the incoming and outgoing result sets -> must dedup to one row.
    incoming = [_r("A", 3, "X", "O"), _r("D", 4, "X", "O")]
    outgoing = [_r("D", 4, "O", "X"), _r("C", 2, "O", "Y")]

    def route(f):
        if isinstance(f.get("to_outlet_id"), list):
            return incoming          # incoming branch: to_outlet_id == scope list
        if isinstance(f.get("from_outlet_id"), list):
            return outgoing          # outgoing branch: from_outlet_id == scope list
        return []

    fake = _FakeMgr(route)
    orig = T.transfer_manager
    T.transfer_manager = fake
    try:
        o = AuthContext(user_id="u", role="OUTLET_MANAGER", scope_level="OUTLET", outlet_id="O")

        items, total = asyncio.run(T._fetch_transfers_scoped(o, {}, limit=2, offset=0))
        # union {A(3), D(4), C(2)} -> sorted desc -> [D, A, C] -> page 1 of 2 -> [D, A]
        assert [i.uid for i in items] == ["D", "A"]
        assert total == 3                                  # deduped count, not 4

        # page 2 continues the same ordering
        page2, total2 = asyncio.run(T._fetch_transfers_scoped(o, {}, limit=2, offset=2))
        assert [i.uid for i in page2] == ["C"]
        assert total2 == 3

        # scope applied as an IN-list over the correct column in each leg
        in_call = next(c for c in fake.calls if isinstance(c.get("to_outlet_id"), list))
        out_call = next(c for c in fake.calls if isinstance(c.get("from_outlet_id"), list))
        assert in_call["to_outlet_id"] == ["O"]
        assert out_call["from_outlet_id"] == ["O"]
    finally:
        T.transfer_manager = orig


if __name__ == "__main__":
    test_transfer_scope_ids_branches()
    test_assert_transfer_in_scope()
    test_global_fetch_passes_filters_straight_through()
    test_scoped_fetch_or_dedup_sort_paginate()
    print("OK")
