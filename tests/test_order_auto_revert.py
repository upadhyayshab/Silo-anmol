"""Pins the daily auto-revert eligibility rule (order_revert_service.should_revert).

An order in a non-terminal, non-pending status reverts to PENDING once its status has
sat untouched for 24h. The pre-existing backlog is anchored at the grace `baseline`
unless `flush` is on. A wrong comparison here either strands stale orders or floods the
pending pool early. Run::

    python tests/test_order_auto_revert.py    # or: pytest tests/test_order_auto_revert.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

from utils.constants import OrderStatus, is_revertible_status  # noqa: E402
from services.order_revert_service import should_revert  # noqa: E402

NOW = datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc)


def ago(hours):
    return NOW - timedelta(hours=hours)


# --- which statuses are in scope -------------------------------------------------
def test_terminal_and_pending_never_revert():
    assert is_revertible_status(OrderStatus.DELIVERED) is False
    assert is_revertible_status(OrderStatus.CANCELLED) is False
    assert is_revertible_status(OrderStatus.PENDING) is False


def test_non_terminal_statuses_revert():
    for s in (OrderStatus.DELIVERY_ALLOTTED, OrderStatus.POSTPONED, OrderStatus.ATTEMPTED,
              OrderStatus.CUSTOMER_NOT_AVAILABLE, OrderStatus.UNABLE_TO_CONTACT,
              OrderStatus.UNABLE_TO_LOCATE, OrderStatus.PAYMENT_NOT_READY):
        assert is_revertible_status(s) is True


# --- the 24h clock ---------------------------------------------------------------
def test_older_than_24h_reverts():
    assert should_revert(ago(25), NOW, baseline=None, flush=False) is True


def test_within_24h_does_not_revert():
    assert should_revert(ago(23), NOW, baseline=None, flush=False) is False


def test_no_clock_does_not_revert():
    assert should_revert(None, NOW, baseline=None, flush=False) is False


# --- grace baseline vs flush -----------------------------------------------------
def test_grace_holds_backlog_until_baseline_ages():
    # 100h-stale order, but grace baseline was set 1h ago -> not yet.
    assert should_revert(ago(100), NOW, baseline=ago(1), flush=False) is False


def test_flush_ignores_baseline():
    assert should_revert(ago(100), NOW, baseline=ago(1), flush=True) is True


def test_backlog_reverts_once_baseline_passes_24h():
    assert should_revert(ago(100), NOW, baseline=ago(25), flush=False) is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nall auto-revert tests passed")
