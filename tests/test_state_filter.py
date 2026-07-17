"""Self-check for the super-admin state filter helper (utils.auth.outlet_ids_for_state).

The resolver turns a state name into the uids of the outlets in that state so GLOBAL
callers can voluntarily narrow order/report views by the *assigned outlet's* state.
The non-trivial bits pinned here: blank input short-circuits without a DB call;
non-blank input is trimmed and matched case-insensitively ($ieq); an empty result is
returned as [] so callers apply the "__none__" sentinel (match nothing, not everything).

DB-free — the OutletManager is faked via `auth._get_outlet_mgr` (mirrors the
monkeypatch style in tests/test_orders_scope.py). Run::

    python tests/test_state_filter.py     # or: pytest tests/test_state_filter.py
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


class _FakeOutletMgr:
    def __init__(self, items):
        self._items = items
        self.last_filters = "UNSET"

    async def fetch_all(self, filters=None, limit=None, **kw):
        self.last_filters = filters
        return SimpleNamespace(items=self._items)


def test_blank_state_returns_empty_without_query():
    from utils import auth
    orig = auth._get_outlet_mgr
    fake = _FakeOutletMgr([SimpleNamespace(uid="o1")])
    auth._get_outlet_mgr = lambda: fake
    try:
        assert asyncio.run(auth.outlet_ids_for_state("")) == []
        assert asyncio.run(auth.outlet_ids_for_state(None)) == []
        assert asyncio.run(auth.outlet_ids_for_state("   ")) == []
        assert fake.last_filters == "UNSET"        # never hit the manager for a blank state
    finally:
        auth._get_outlet_mgr = orig


def test_state_resolves_to_outlet_uids_case_insensitive():
    from utils import auth
    orig = auth._get_outlet_mgr
    fake = _FakeOutletMgr([SimpleNamespace(uid="o1"), SimpleNamespace(uid="o2")])
    auth._get_outlet_mgr = lambda: fake
    try:
        ids = asyncio.run(auth.outlet_ids_for_state("  Karnataka "))
        assert ids == ["o1", "o2"]                 # returns the outlet uids
        # Trimmed + lowercased before the query — equivalent, since $ieq is ilike
        # (case-insensitive); the lowercase fold came with multi-state/alias support.
        assert fake.last_filters == {"state": {"$ieq": "karnataka"}}
    finally:
        auth._get_outlet_mgr = orig


def test_state_with_no_outlets_returns_empty_list():
    from utils import auth
    orig = auth._get_outlet_mgr
    fake = _FakeOutletMgr([])
    auth._get_outlet_mgr = lambda: fake
    try:
        # empty -> [], so a caller doing `ids or ["__none__"]` matches nothing (not everything)
        assert asyncio.run(auth.outlet_ids_for_state("nowhere")) == []
    finally:
        auth._get_outlet_mgr = orig


if __name__ == "__main__":
    test_blank_state_returns_empty_without_query()
    test_state_resolves_to_outlet_uids_case_insensitive()
    test_state_with_no_outlets_returns_empty_list()
    print("OK")
