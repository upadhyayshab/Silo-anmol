"""Transfers must carry a concrete source AND destination (no NULL warehouse).

DB-free: only exercises the pydantic request models, so it runs without the
app's config/DB stack. Run with::

    python tests/test_transfer_source_required.py
    # or: pytest tests/test_transfer_source_required.py
"""
import os
import sys

# Make `app/` importable without booting the full app.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from pydantic import ValidationError  # noqa: E402

from models import (  # noqa: E402
    StockTransferCreateRequest,
    StockTransferCreateRequestBulk,
)


def _rejects(model, **kwargs):
    try:
        model(**kwargs)
    except ValidationError:
        return True
    return False


def test_single_create_requires_source_and_destination():
    item = {"product_id": "products_x", "quantity_requested": 1}

    # Valid: both ends present
    ok = StockTransferCreateRequest(
        from_outlet_id="outlets_src", to_outlet_id="outlets_dst", items=[item]
    )
    assert ok.from_outlet_id == "outlets_src"

    # Missing source -> rejected
    assert _rejects(StockTransferCreateRequest, to_outlet_id="outlets_dst", items=[item])
    # Empty source -> rejected (min_length=1)
    assert _rejects(
        StockTransferCreateRequest,
        from_outlet_id="", to_outlet_id="outlets_dst", items=[item],
    )
    # Empty destination -> rejected
    assert _rejects(
        StockTransferCreateRequest,
        from_outlet_id="outlets_src", to_outlet_id="", items=[item],
    )


def test_bulk_create_requires_source_and_destination():
    item = {"sku": "SKU-1", "quantity_requested": 1}

    ok = StockTransferCreateRequestBulk(
        from_outlet_name="Hassan", to_outlet_name="Hubli", items=[item]
    )
    assert ok.from_outlet_name == "Hassan"

    # Missing / empty source name -> rejected
    assert _rejects(StockTransferCreateRequestBulk, to_outlet_name="Hubli", items=[item])
    assert _rejects(
        StockTransferCreateRequestBulk,
        from_outlet_name="", to_outlet_name="Hubli", items=[item],
    )


if __name__ == "__main__":
    test_single_create_requires_source_and_destination()
    test_bulk_create_requires_source_and_destination()
    print("All transfer source-required checks passed.")
