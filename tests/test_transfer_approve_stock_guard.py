"""Approval must be blocked when the source can't cover the transfer.

Requests are allowed through with warnings (create path); the guard
`assert_source_stock_available` is what stops a short transfer from being approved.

Exercises the guard directly with in-memory fake managers — no DB. Run with::

    python tests/test_transfer_approve_stock_guard.py
    # or: pytest tests/test_transfer_approve_stock_guard.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:  # path_setup prints a banner; keep it from blowing up a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — wires SharedBackend; must precede router import
import routers.v1.transfers as t  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _Box:
    """Wraps a list as `.items` (mirrors manager fetch_all responses)."""
    def __init__(self, items):
        self.items = items


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _patch(monkey):
    """monkey = dict of {product_id: available_qty}; None means no stock record."""
    async def fetch_transfer(_id):
        return _Obj(from_outlet_id="outlets_src")

    async def fetch_outlet(_id):
        return _Obj(outlet_type="outlet")  # not FACTORY

    async def fetch_items(filters=None):
        return _Box([_Obj(product_id=pid, quantity_requested=5) for pid in monkey])

    async def fetch_inventory(filters=None):
        pid = filters["product_id"]
        qty = monkey[pid]
        return _Box([] if qty is None else [_Obj(quantity=qty)])

    async def fetch_product(_id):
        return _Obj(product_name=f"P-{_id}")

    async def warehouse_id(_engine):
        return "outlets_wh"

    t.transfer_manager.fetch = fetch_transfer
    t.outlet_manager.fetch = fetch_outlet
    t.transfer_item_manager.fetch_all = fetch_items
    t.inventory_manager.fetch_all = fetch_inventory
    t.product_manager.fetch = fetch_product
    t.get_default_warehouse_id = warehouse_id


def _raises(coro):
    try:
        asyncio.run(coro)
    except HTTPException as e:
        assert e.status_code == 400
        return True
    return False


def test_guard_blocks_and_allows():
    # Enough stock for both products (requested 5 each) -> no raise
    _patch({"prod_a": 10, "prod_b": 5})
    assert not _raises(t.assert_source_stock_available("tr1"))

    # One product short -> raise
    _patch({"prod_a": 10, "prod_b": 4})
    assert _raises(t.assert_source_stock_available("tr1"))

    # No stock record at all -> raise
    _patch({"prod_a": None})
    assert _raises(t.assert_source_stock_available("tr1"))

    # quantities override lowers the need below available -> no raise
    _patch({"prod_a": 3})
    assert not _raises(t.assert_source_stock_available("tr1", quantities={"prod_a": 3}))
    # ...and raises it above available -> raise
    assert _raises(t.assert_source_stock_available("tr1", quantities={"prod_a": 99}))


def test_factory_source_always_passes():
    _patch({"prod_a": None})  # zero stock everywhere

    async def factory_outlet(_id):
        return _Obj(outlet_type=t.OutletType.FACTORY)

    t.outlet_manager.fetch = factory_outlet
    assert not _raises(t.assert_source_stock_available("tr1"))


if __name__ == "__main__":
    test_guard_blocks_and_allows()
    test_factory_source_always_passes()
    print("All transfer approve-stock-guard checks passed.")
