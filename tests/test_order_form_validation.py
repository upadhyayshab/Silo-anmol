"""Create-Order form guardrails (Task 1): prepaid orders must carry a transaction reference,
and a per-unit discount can never exceed the product's selling price (cost_price). Both checks
are DB-free — the payment check is a Pydantic validator on OrderPaymentRequest, the discount
check is a pure helper in orders.py (mirrors payment_status_for / _apply_order_scope). Run::

    python -m pytest tests/test_order_form_validation.py -q
"""
import os
import sys
from decimal import Decimal

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import path_setup  # noqa: F401,E402  — must precede model/router imports

from models import OrderPaymentRequest  # noqa: E402


# --- 1a: transaction_reference required for prepaid payment methods --------------------

def test_prepaid_upi_without_transaction_reference_is_rejected():
    with pytest.raises(ValidationError) as exc_info:
        OrderPaymentRequest(payment_method="upi", amount_paid=Decimal("500"))
    assert "transaction_reference is required for prepaid payments" in str(exc_info.value)


def test_prepaid_online_with_blank_transaction_reference_is_rejected():
    with pytest.raises(ValidationError) as exc_info:
        OrderPaymentRequest(payment_method="online", amount_paid=Decimal("500"), transaction_reference="   ")
    assert "transaction_reference is required for prepaid payments" in str(exc_info.value)


def test_prepaid_card_without_transaction_reference_is_rejected():
    with pytest.raises(ValidationError):
        OrderPaymentRequest(payment_method="card", amount_paid=Decimal("500"))


def test_prepaid_with_transaction_reference_passes_validation():
    payment = OrderPaymentRequest(
        payment_method="upi", amount_paid=Decimal("500"), transaction_reference="TXN12345"
    )
    assert payment.transaction_reference == "TXN12345"


def test_cash_payment_without_transaction_reference_is_allowed():
    payment = OrderPaymentRequest(payment_method="cash", amount_paid=Decimal("500"))
    assert payment.transaction_reference is None


# --- 1b: per-unit discount cannot exceed the product's selling price (cost_price) ------

def test_discount_exceeding_selling_price_is_rejected():
    import routers.v1.orders as O
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        O._assert_discount_within_selling_price("Ghee 1L", Decimal("150.00"), Decimal("100.00"))
    assert exc_info.value.status_code == 400
    assert "Ghee 1L" in exc_info.value.detail
    assert "exceeds its selling price" in exc_info.value.detail


def test_discount_equal_to_selling_price_is_allowed():
    # Boundary: discount == selling price must be ALLOWED (item total can be exactly 0).
    import routers.v1.orders as O

    O._assert_discount_within_selling_price("Ghee 1L", Decimal("100.00"), Decimal("100.00"))  # no raise


def test_discount_below_selling_price_is_allowed():
    import routers.v1.orders as O

    O._assert_discount_within_selling_price("Ghee 1L", Decimal("50.00"), Decimal("100.00"))  # no raise


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
