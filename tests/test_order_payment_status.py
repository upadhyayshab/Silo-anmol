"""Pins the prepaid payment_status rule used when an order records its payment atomically
(orders.py create_order). Fully PAID only when the payment covers the post-discount order
value; anything short is PARTIALLY_PAID. Money path — a wrong comparison base silently
mislabels payments. Run::

    python tests/test_order_payment_status.py    # or: pytest tests/test_order_payment_status.py
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from utils.constants import PaymentStatus, payment_status_for  # noqa: E402


def test_exact_amount_is_paid():
    assert payment_status_for(Decimal("500"), Decimal("500")) == PaymentStatus.PAID


def test_overpay_is_paid():
    assert payment_status_for(Decimal("600"), Decimal("500")) == PaymentStatus.PAID


def test_short_is_partial():
    assert payment_status_for(Decimal("200"), Decimal("500")) == PaymentStatus.PARTIALLY_PAID


def test_base_is_post_discount_value_not_remaining():
    # 4000 paid on a 5000 order still leaves 1000 -> must be PARTIALLY_PAID.
    # (The old frontend compared against total_amount, already net of prepaid, and would
    # have wrongly called this PAID.)
    assert payment_status_for(Decimal("4000"), Decimal("5000")) == PaymentStatus.PARTIALLY_PAID


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all payment_status checks passed")
