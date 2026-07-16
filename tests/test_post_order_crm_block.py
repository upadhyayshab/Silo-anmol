"""T0.1 + T5.1 — the post-order CRM block in orders.py (DB-free, fake-swap).

Drives the REAL create_order / create_proxy_order route coroutines end-to-end (no
FastAPI/DB — module-level managers are swapped for fakes, mirroring
tests/test_order_lifecycle_endpoints.py's pattern) to pin two things in the SAME
block that calls leadService.handle_post_order/attribute_order:

  1. (T0.1) A CRM-side failure there is now LOGGED (with lead/order context) instead
     of silently swallowed by a bare `except: print(...)` — and, just as important,
     it still must NOT propagate: the order itself already committed before this
     block runs, so the HTTP call must still return a normal OrderResponse.
  2. (T5.1) The booking telecaller becomes the lead's owner via
     leadService.reassign_to_booker — create_order books as the logged-in
     telecaller, create_proxy_order books as payload.telecaller_id (not the admin
     caller).

Run::

    python tests/test_post_order_crm_block.py     # or: pytest tests/test_post_order_crm_block.py
"""
import os
import sys
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:  # path_setup prints a banner; keep it from blowing up a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports


def run(coro):
    return asyncio.run(coro)


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        pass


class FakeOrderManager:
    """Enough of CustomerOrderManager for create_order/create_proxy_order to run."""

    def __init__(self):
        self.updates = []
        self._n = 0

    def session_factory(self):
        return _FakeSession()

    async def create(self, order, session=None, **kw):
        self._n += 1
        order.uid = f"order-{self._n}"
        order.created_at = datetime.now(timezone.utc)
        return order

    async def update(self, uid, updates, **kw):
        self.updates.append((uid, updates))
        return None


class FakeOrderItemManager:
    def __init__(self):
        self._n = 0

    async def create(self, item, **kw):
        self._n += 1
        item.uid = f"item-{self._n}"
        return item


class FakeProductManager:
    async def fetch(self, product_id):
        return SimpleNamespace(
            uid=product_id, product_name="Test Product", is_active=True,
            cost_price=Decimal("100.00"), unit_price=Decimal("100.00"),
            margin=Decimal("0.00"), commission=Decimal("0.00"),
        )


class FakeUserManager:
    def __init__(self, users=None):
        self.users = users or {}

    async def fetch(self, uid):
        return self.users.get(uid, SimpleNamespace(uid=uid, agency_id=None, is_active=True,
                                                    full_name="Fake Telecaller",
                                                    role="TELECALLER"))


def _payload_kwargs(**overrides):
    from models import OrderCreateRequest, OrderItemRequest
    from utils.constants import CollectionType, PaymentMethod
    base = dict(
        customer_name="Test Customer", customer_phone="9876543210",
        address_line="1 Test Street", district="Hassan", state="karnataka",
        pincode="573201", collection_type=CollectionType.DOORSTEP,
        payment_method=PaymentMethod.CASH,
        items=[OrderItemRequest(product_id="p1", quantity=1)],
        lead_id="lead-1",
    )
    base.update(overrides)
    return OrderCreateRequest(**base)


def _install_fakes(O):
    """Swap orders.py's module-level managers/helpers for fakes. Returns (fakes, restore)."""
    fom = FakeOrderManager()
    foim = FakeOrderItemManager()
    fpm = FakeProductManager()
    fum = FakeUserManager()

    async def _no_outlet(*a, **kw):
        return None

    orig = (O.order_manager, O.order_item_manager, O.product_manager, O.user_manager,
           O.auto_assign_outlet)
    O.order_manager = fom
    O.order_item_manager = foim
    O.product_manager = fpm
    O.user_manager = fum
    O.auto_assign_outlet = _no_outlet

    import services.order_events_service as OES
    orig_record_event = OES.record_event

    async def _no_record_event(*a, **kw):
        return None
    OES.record_event = _no_record_event

    def _restore():
        (O.order_manager, O.order_item_manager, O.product_manager, O.user_manager,
         O.auto_assign_outlet) = orig
        OES.record_event = orig_record_event

    return SimpleNamespace(order_manager=fom, order_item_manager=foim), _restore


# --- T0.1: exception in the CRM block is logged, order creation still succeeds ----

def test_create_order_post_order_failure_logs_and_still_succeeds(caplog):
    import routers.v1.orders as O
    import services.leadService as leadService
    from fastapi import BackgroundTasks
    from utils.auth import AuthContext

    _, restore = _install_fakes(O)
    orig_handle_post_order = leadService.handle_post_order
    orig_record_activity = leadService.record_activity
    orig_attribute_order = leadService.attribute_order
    orig_reassign = leadService.reassign_to_booker

    async def _fake_record_activity(*a, **kw):
        return None

    async def _boom(*a, **kw):
        raise RuntimeError("boom: CRM side failure")

    leadService.record_activity = _fake_record_activity
    leadService.handle_post_order = _boom
    leadService.attribute_order = _fake_record_activity   # unreachable but harmless if it were reached
    leadService.reassign_to_booker = _fake_record_activity  # unreachable but harmless if it were reached

    try:
        payload = _payload_kwargs()
        ctx = AuthContext(user_id="tc-1", role="TELECALLER", scope_level="OUTLET")
        with caplog.at_level(logging.ERROR, logger="routers.v1.orders"):
            resp = run(O.create_order(payload, BackgroundTasks(), ctx))
    finally:
        restore()
        leadService.handle_post_order = orig_handle_post_order
        leadService.record_activity = orig_record_activity
        leadService.attribute_order = orig_attribute_order
        leadService.reassign_to_booker = orig_reassign

    # Order creation must have succeeded despite the CRM-block exception.
    assert resp.uid == "order-1"
    assert resp.order_number

    # The failure must be LOGGED (not silently swallowed) with lead/order context.
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected the post-order CRM failure to be logged"
    joined = "\n".join(r.getMessage() for r in error_records)
    assert "lead-1" in joined and "order-1" in joined
    assert any(r.exc_info for r in error_records), "expected the traceback to be captured (logger.exception)"


def test_create_proxy_order_post_order_failure_logs_and_still_succeeds(caplog):
    import routers.v1.orders as O
    import services.leadService as leadService
    from fastapi import BackgroundTasks
    from utils.auth import AuthContext

    _, restore = _install_fakes(O)
    telecaller = SimpleNamespace(uid="tc-2", agency_id=None, is_active=True,
                                 full_name="Proxy TC", role="TELECALLER")
    O.user_manager.users = {"tc-2": telecaller}

    orig_handle_post_order = leadService.handle_post_order
    orig_record_activity = leadService.record_activity
    orig_attribute_order = leadService.attribute_order
    orig_reassign = leadService.reassign_to_booker

    async def _fake_record_activity(*a, **kw):
        return None

    async def _boom(*a, **kw):
        raise RuntimeError("boom: CRM side failure")

    leadService.record_activity = _fake_record_activity
    leadService.handle_post_order = _boom
    leadService.attribute_order = _fake_record_activity
    leadService.reassign_to_booker = _fake_record_activity

    try:
        from models import ProxyOrderCreateRequest
        payload = ProxyOrderCreateRequest(**_payload_kwargs().model_dump(), telecaller_id="tc-2")
        ctx = AuthContext(user_id="admin-1", role="ADMIN", scope_level="GLOBAL")
        with caplog.at_level(logging.ERROR, logger="routers.v1.orders"):
            resp = run(O.create_proxy_order(payload, BackgroundTasks(), ctx))
    finally:
        restore()
        leadService.handle_post_order = orig_handle_post_order
        leadService.record_activity = orig_record_activity
        leadService.attribute_order = orig_attribute_order
        leadService.reassign_to_booker = orig_reassign

    assert resp.uid == "order-1"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected the proxy post-order CRM failure to be logged"
    joined = "\n".join(r.getMessage() for r in error_records)
    assert "lead-1" in joined and "order-1" in joined
    assert any(r.exc_info for r in error_records)


# --- T5.1: end-to-end — booking telecaller is passed to reassign_to_booker --------

def test_create_order_calls_reassign_with_booking_telecaller():
    import routers.v1.orders as O
    import services.leadService as leadService
    from fastapi import BackgroundTasks
    from utils.auth import AuthContext

    _, restore = _install_fakes(O)
    orig_handle_post_order = leadService.handle_post_order
    orig_record_activity = leadService.record_activity
    orig_attribute_order = leadService.attribute_order
    orig_reassign = leadService.reassign_to_booker

    calls = []

    async def _noop(*a, **kw):
        return None

    async def _fake_reassign(engine, lead_id, booker_id, booker_role):
        calls.append((lead_id, booker_id, booker_role))

    leadService.record_activity = _noop
    leadService.handle_post_order = _noop
    leadService.attribute_order = _noop
    leadService.reassign_to_booker = _fake_reassign

    try:
        payload = _payload_kwargs()
        ctx = AuthContext(user_id="tc-1", role="TELECALLER", scope_level="OUTLET")
        run(O.create_order(payload, BackgroundTasks(), ctx))
    finally:
        restore()
        leadService.handle_post_order = orig_handle_post_order
        leadService.record_activity = orig_record_activity
        leadService.attribute_order = orig_attribute_order
        leadService.reassign_to_booker = orig_reassign

    # create_order books as the logged-in telecaller (current_user_id / ctx.role), not
    # an admin caller.
    assert calls == [("lead-1", "tc-1", "TELECALLER")], calls


def test_create_proxy_order_calls_reassign_with_target_telecaller_not_admin():
    import routers.v1.orders as O
    import services.leadService as leadService
    from fastapi import BackgroundTasks
    from utils.auth import AuthContext

    _, restore = _install_fakes(O)
    telecaller = SimpleNamespace(uid="tc-2", agency_id=None, is_active=True,
                                 full_name="Proxy TC", role="TELECALLER")
    O.user_manager.users = {"tc-2": telecaller}

    orig_handle_post_order = leadService.handle_post_order
    orig_record_activity = leadService.record_activity
    orig_attribute_order = leadService.attribute_order
    orig_reassign = leadService.reassign_to_booker

    calls = []

    async def _noop(*a, **kw):
        return None

    async def _fake_reassign(engine, lead_id, booker_id, booker_role):
        calls.append((lead_id, booker_id, booker_role))

    leadService.record_activity = _noop
    leadService.handle_post_order = _noop
    leadService.attribute_order = _noop
    leadService.reassign_to_booker = _fake_reassign

    try:
        from models import ProxyOrderCreateRequest
        payload = ProxyOrderCreateRequest(**_payload_kwargs().model_dump(), telecaller_id="tc-2")
        # The ADMIN calls the endpoint on behalf of the telecaller — booker must be
        # the TARGET telecaller (tc-2), never the admin (admin-1).
        ctx = AuthContext(user_id="admin-1", role="ADMIN", scope_level="GLOBAL")
        run(O.create_proxy_order(payload, BackgroundTasks(), ctx))
    finally:
        restore()
        leadService.handle_post_order = orig_handle_post_order
        leadService.record_activity = orig_record_activity
        leadService.attribute_order = orig_attribute_order
        leadService.reassign_to_booker = orig_reassign

    assert calls == [("lead-1", "tc-2", "TELECALLER")], calls


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
