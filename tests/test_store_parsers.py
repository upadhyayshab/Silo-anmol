"""Pure-function tests for the native-Medusa parsers. No DB required.

Run: python tests/test_store_parsers.py   (or pytest)
Field paths target Medusa v2; confirm against a real webhook sample.
"""
import sys
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).parents[1] / "app" / "services"))

from storeOrderService import parse_medusa_order, parse_medusa_customer

SAMPLE_ORDER = {
    "id": "order_01ABC",
    "email": "buyer@example.com",
    "payment_status": "captured",
    "total": "1499.00",
    "shipping_address": {
        "first_name": "Asha", "last_name": "Rao", "phone": "+919535328180",
        "address_1": "12 MG Road", "address_2": "Near Park",
        "city": "Hassan", "province": "Karnataka", "postal_code": "573201",
    },
    "items": [
        {"title": "Gausampurna 50kg", "quantity": 2, "unit_price": "699.00",
         "variant": {"sku": "GS-50"}},
    ],
}

SAMPLE_CUSTOMER = {
    "id": "cus_01XYZ", "email": "buyer@example.com",
    "first_name": "Asha", "last_name": "Rao", "phone": "+919535328180",
    "addresses": [{"address_1": "12 MG Road", "city": "Hassan",
                   "province": "Karnataka", "postal_code": "573201"}],
}


def test_parse_order():
    o = parse_medusa_order(SAMPLE_ORDER)
    assert o["medusa_id"] == "order_01ABC"
    assert o["customer_name"] == "Asha Rao"
    assert o["mobile"] == "+919535328180"
    assert o["pincode"] == "573201"
    assert o["state"] == "Karnataka"
    assert o["city"] == "Hassan"
    assert o["total"] == Decimal("1499.00")
    assert o["items"] == [{"sku": "GS-50", "quantity": 2,
                           "unit_price": Decimal("699.00"), "title": "Gausampurna 50kg"}]


def test_parse_order_name_falls_back_to_email():
    payload = {"id": "o1", "email": "x@y.com", "total": "0",
               "shipping_address": {"phone": "9999999999"}, "items": []}
    o = parse_medusa_order(payload)
    assert o["customer_name"] == "x@y.com"
    assert o["mobile"] == "9999999999"


def test_parse_customer():
    c = parse_medusa_customer(SAMPLE_CUSTOMER)
    assert c["customer_name"] == "Asha Rao"
    assert c["mobile"] == "+919535328180"
    assert c["pincode"] == "573201"


if __name__ == "__main__":
    test_parse_order()
    test_parse_order_name_falls_back_to_email()
    test_parse_customer()
    print("OK")
