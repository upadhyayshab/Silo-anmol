"""GET /config must not 500 on a config row with NULL gstin/pan.

The update path (SystemConfigurationUpdateRequest) accepts these as Optional, so a row can
legitimately hold NULL. Requiring `str` on the response made GET /config raise a Pydantic
validation error -> caught by config.py's blanket except -> 500. That was a deadlock: /config
backs the very page you'd use to set those values.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
import path_setup  # noqa: F401,E402  — must precede model imports

from datetime import datetime, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402

from models import SystemConfigurationResponse  # noqa: E402


def _row(**over):
    base = dict(
        uid="cfg1", company_name="Silo", company_address="Addr",
        company_gstin=None, company_pan=None,
        cgst_default_rate=Decimal("9.00"), sgst_default_rate=Decimal("9.00"),
        igst_default_rate=Decimal("18.00"), price_includes_tax=False,
        created_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return base


def test_null_gstin_and_pan_validate_to_none():
    resp = SystemConfigurationResponse(**_row())
    assert resp.company_gstin is None
    assert resp.company_pan is None


def test_real_gstin_and_pan_still_round_trip():
    resp = SystemConfigurationResponse(**_row(company_gstin="29AAAAA0000A1Z5", company_pan="AAAAA0000A"))
    assert resp.company_gstin == "29AAAAA0000A1Z5"
    assert resp.company_pan == "AAAAA0000A"
