"""Regression guard for the CRM order->lead link (checkpoint 3.5).

DB-free: only checks the enum value and request-model field that wire a
CRM-placed order onto a lead's timeline. Run with::

    python tests/test_order_lead_link.py
    # or
    pytest tests/test_order_lead_link.py
"""
import os
import sys

# Make `app/` importable without booting the full app/DB stack.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from utils.crm_enums import LeadActivityType  # noqa: E402
from models.erpModels import OrderCreateRequest  # noqa: E402


def test_order_activity_type_exists():
    # The timeline icon/label map and the create_order hook both key off this.
    assert LeadActivityType.ORDER.value == "order"


def test_order_create_request_accepts_lead_id():
    field = OrderCreateRequest.model_fields.get("lead_id")
    assert field is not None, "OrderCreateRequest must carry lead_id to link the order"
    # Optional -- most orders have no lead, so it must default to None.
    assert field.default is None


if __name__ == "__main__":
    test_order_activity_type_exists()
    test_order_create_request_accepts_lead_id()
    print("OK")
