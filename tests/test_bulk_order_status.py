"""Guarded bulk order status update (DB-free).

POST /orders/bulk/status-update lets the super admin (orders:revoke) set many
orders' status from an uploaded file. Server-side typed guard: confirm_text
must equal the target status name. Transition rules are the single-endpoint
ones plus one bulk-only edge — CANCELLED -> PENDING (un-cancel). DELIVERED is
immutable in bulk; bad rows are skipped with a reason, never aborting the batch.

Mirrors tests/test_orders_scope.py: import path_setup first, fake the module
globals (order_manager / store_service / tracking_manager) on routers.v1.orders
and restore in finally. Run::

    python tests/test_bulk_order_status.py    # or: pytest tests/test_bulk_order_status.py
"""
import os
import sys
import asyncio
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:  # path_setup prints a banner; keep it from blowing up a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports


def test_transition_allowed_uncancel_edge():
    import routers.v1.orders as O
    from utils.constants import OrderStatus

    # normal rules still apply
    assert O._transition_allowed(OrderStatus.PENDING, OrderStatus.CANCELLED)
    assert not O._transition_allowed(OrderStatus.CANCELLED, OrderStatus.PENDING)
    # the bulk-only edge: un-cancel back to pending
    assert O._transition_allowed(OrderStatus.CANCELLED, OrderStatus.PENDING, allow_uncancel=True)
    # the edge is ONLY cancelled->pending — nothing else widens
    assert not O._transition_allowed(OrderStatus.CANCELLED, OrderStatus.DELIVERY_ALLOTTED, allow_uncancel=True)
    # delivered stays final even with the flag
    assert not O._transition_allowed(OrderStatus.DELIVERED, OrderStatus.PENDING, allow_uncancel=True)
    assert not O._transition_allowed(OrderStatus.DELIVERED, OrderStatus.CANCELLED, allow_uncancel=True)


if __name__ == "__main__":
    for _name in sorted(list(globals())):
        if _name.startswith("test_") and callable(globals()[_name]):
            globals()[_name]()
            print(f"PASS {_name}")
