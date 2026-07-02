"""Telecaller-created leads with no explicit source default to source='Telecaller'.

DB-free: monkeypatches leadService.create_lead / build_lead_response so the
router's `create_lead` endpoint function can be exercised directly (mirrors
tests/test_orders_scope.py's pattern for routers/v1/orders.py). Run with::

    python tests/test_lead_source_telecaller.py
    # or: pytest tests/test_lead_source_telecaller.py
"""
import os
import sys
import asyncio
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:  # path_setup prints a banner with a unicode glyph; keep it from blowing up a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports


def _fake_response():
    return SimpleNamespace(status_code=None)


def _run_create_lead(payload, role):
    import routers.v1.leads as L
    from utils.auth import AuthContext

    captured = {}

    async def _fake_create_lead(engine, payload, by_user_id, **kwargs):
        captured["payload"] = payload
        lead = SimpleNamespace(uid="leads_1", owner_id=by_user_id)
        return lead, True

    async def _fake_build_lead_response(engine, lead, **kwargs):
        return {"uid": lead.uid, "source": captured["payload"].source}

    orig_create = L.leadService.create_lead
    orig_build = L.leadService.build_lead_response
    L.leadService.create_lead = _fake_create_lead
    L.leadService.build_lead_response = _fake_build_lead_response
    try:
        ctx = AuthContext(user_id="u", role=role, scope_level="OUTLET")
        asyncio.run(L.create_lead(payload, _fake_response(), ctx))
    finally:
        L.leadService.create_lead = orig_create
        L.leadService.build_lead_response = orig_build

    return captured["payload"]


def test_telecaller_no_source_defaults_to_telecaller():
    from models import LeadCreateRequest

    payload = LeadCreateRequest(first_name="A", mobile="9999999999")
    result = _run_create_lead(payload, role="TELECALLER")
    assert result.source.value == "Telecaller"


def test_agency_telecaller_no_source_defaults_to_telecaller():
    from models import LeadCreateRequest

    payload = LeadCreateRequest(first_name="A", mobile="9999999998")
    result = _run_create_lead(payload, role="AGENCY_TELECALLER")
    assert result.source.value == "Telecaller"


def test_telecaller_explicit_source_not_overridden():
    from models import LeadCreateRequest

    payload = LeadCreateRequest(first_name="A", mobile="9999999997", source="Outlet")
    result = _run_create_lead(payload, role="TELECALLER")
    assert result.source.value == "Outlet"


def test_non_telecaller_no_source_not_defaulted():
    from models import LeadCreateRequest

    payload = LeadCreateRequest(first_name="A", mobile="9999999996")
    result = _run_create_lead(payload, role="ADMIN")
    assert result.source is None


if __name__ == "__main__":
    test_telecaller_no_source_defaults_to_telecaller()
    test_agency_telecaller_no_source_defaults_to_telecaller()
    test_telecaller_explicit_source_not_overridden()
    test_non_telecaller_no_source_not_defaulted()
    print("OK")
