"""DB-free unit tests for the Medusa -> ERP store parsers.

Exercises the pure parse functions (no DB/config), especially the new optional
`channel` (app vs website) mapping and its non-breaking absence. Run with::

    python tests/test_store_order_parse.py
    # or, if pytest is installed:
    pytest tests/test_store_order_parse.py
"""
import importlib.util
import os
import unittest
from decimal import Decimal

# Load storeOrderService.py directly, bypassing services/__init__.py (which
# imports the full DB/config stack). The parsers use stdlib only.
_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "services",
                     "storeOrderService.py")
_spec = importlib.util.spec_from_file_location("storeOrderService_pure", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # noqa: E402
parse_medusa_order = _mod.parse_medusa_order
parse_medusa_customer = _mod.parse_medusa_customer


def _order(**over):
    base = {
        "id": "order_123",
        "email": "buyer@example.com",
        "total": "499.50",
        "payment_status": "captured",
        "metadata": {"channel": "app"},
        "shipping_address": {
            "first_name": "Asha", "last_name": "Rao", "phone": "9876543210",
            "address_1": "12 MG Road", "city": "Hassan",
            "province": "karnataka", "postal_code": "573201",
        },
        "items": [
            {"quantity": 2, "unit_price": "100.00", "title": "Ghee 500ml",
             "variant": {"sku": "GHEE-500"}},
        ],
    }
    base.update(over)
    return base


class TestParseMedusaOrder(unittest.TestCase):
    def test_channel_from_metadata(self):
        self.assertEqual("app", parse_medusa_order(_order())["channel"])
        self.assertEqual(
            "website",
            parse_medusa_order(_order(metadata={"channel": "website"}))["channel"],
        )

    def test_channel_optional_absent(self):
        # Non-breaking: no metadata / no channel -> None, nothing raised.
        self.assertIsNone(parse_medusa_order(_order(metadata={}))["channel"])
        self.assertIsNone(parse_medusa_order(_order(metadata=None))["channel"])
        o = _order()
        del o["metadata"]
        self.assertIsNone(parse_medusa_order(o)["channel"])

    def test_core_fields_and_decimal(self):
        p = parse_medusa_order(_order())
        self.assertEqual("order_123", p["medusa_id"])
        self.assertEqual("Asha Rao", p["customer_name"])
        self.assertEqual("9876543210", p["mobile"])
        self.assertEqual(Decimal("499.50"), p["total"])
        self.assertEqual("karnataka", p["state"])

    def test_items_resolved_by_sku(self):
        items = parse_medusa_order(_order())["items"]
        self.assertEqual(1, len(items))
        self.assertEqual("GHEE-500", items[0]["sku"])
        self.assertEqual(2, items[0]["quantity"])
        self.assertEqual(Decimal("100.00"), items[0]["unit_price"])

    def test_item_without_sku_is_dropped(self):
        o = _order(items=[{"quantity": 1, "unit_price": "5", "variant": {}}])
        self.assertEqual([], parse_medusa_order(o)["items"])

    def test_name_falls_back_to_email(self):
        o = _order(shipping_address={"phone": "9876543210"})
        self.assertEqual("buyer@example.com", parse_medusa_order(o)["customer_name"])

    def test_bad_total_defaults_to_zero(self):
        self.assertEqual(Decimal("0"), parse_medusa_order(_order(total="abc"))["total"])


class TestParseMedusaCustomer(unittest.TestCase):
    def test_basic_with_address(self):
        p = parse_medusa_customer({
            "first_name": "Ravi", "last_name": None, "phone": "9999988888",
            "email": "ravi@example.com",
            "addresses": [{"address_1": "9 Cross", "city": "Mysore",
                           "province": "karnataka", "postal_code": "570001"}],
        })
        self.assertEqual("Ravi", p["customer_name"])
        self.assertEqual("9999988888", p["mobile"])
        self.assertEqual("Mysore", p["city"])
        self.assertEqual("karnataka", p["state"])

    def test_no_addresses(self):
        p = parse_medusa_customer(
            {"first_name": "A", "phone": "9", "email": "a@b.com"})
        self.assertIsNone(p["city"])
        self.assertEqual("9", p["mobile"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
