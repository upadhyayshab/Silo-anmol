"""Pins the Meta CAPI event builder (services/facebook_capi.build_event) — DB-free, no network.

What matters and why it's tested:
- Stage -> Meta event names are VERBATIM to the live dataset, including the legacy
  misspellings (`Engagged`, `Not reacheable`). "Fixing" a typo silently breaks the existing
  custom conversions / ad optimization, so pin them exactly.
- Every LeadStage must map to an event name, or that stage fires nothing (silent attribution gap).
- PII (phone/email) must be NORMALIZED then SHA-256 hashed per Meta spec, under fields `em`/`ph`,
  phone as India E.164 digits (`91` + national, no `+`). A normalization regression changes the
  hash and silently kills Meta match rates — so assert the exact hash.
- event_id must be deterministic (`{uid}:{name}:{YYYYMMDD}`) — it's the dedup key across
  backfill / LSQ overlap; non-determinism would double-count conversions.

Run::

    python tests/test_facebook_capi.py        # or: pytest tests/test_facebook_capi.py
"""
import hashlib
import importlib.util
import os
import sys
import time
from types import SimpleNamespace

_APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, _APP)

# Load facebook_capi.py directly: importing it via `services.facebook_capi` would run
# services/__init__.py, which pulls the whole package (and SharedBackend) just to test one
# pure module. The module's own imports (config/utils) are light and DB-free.
_spec = importlib.util.spec_from_file_location(
    "facebook_capi", os.path.join(_APP, "services", "facebook_capi.py"))
_fbcapi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fbcapi)
build_event, STAGE_EVENT, _sha256 = _fbcapi.build_event, _fbcapi.STAGE_EVENT, _fbcapi._sha256

from utils.crm_enums import LeadStage  # noqa: E402


def _lead(uid="lead-1", mobile=None, email=None, leadgen_id=None):
    return SimpleNamespace(
        uid=uid, mobile=mobile, email=email,
        campaign_data={"leadgen_id": leadgen_id} if leadgen_id else None,
    )


def _sha(s):  # independent of _sha256 — proves the value, not just "it called the helper"
    return hashlib.sha256(s.encode()).hexdigest()


# --- stage -> event-name mapping -------------------------------------------------------------

def test_every_stage_is_mapped_except_deliberate_exclusions():
    # A new LeadStage added without a CAPI event name would silently fire nothing —
    # any exclusion must be listed here ON PURPOSE with its reason.
    # MAX_CALL_DONE (added 7b9c3cd): internal ">=20 not-connected calls" cap, not a
    # conversion signal — Meta gets nothing for it, by design (verified 2026-07-17).
    excluded = {LeadStage.MAX_CALL_DONE}
    assert set(STAGE_EVENT) == set(LeadStage) - excluded, "STAGE_EVENT drifted from LeadStage"
    # The excluded stage degrades to a no-op, not a crash.
    assert build_event(_lead(mobile="9876543210"), LeadStage.MAX_CALL_DONE, event_time=0) is None


def test_misspellings_preserved_verbatim():
    # These typos are intentional — they match the live dataset's custom conversions.
    assert STAGE_EVENT[LeadStage.ENGAGED] == "Engagged"
    assert STAGE_EVENT[LeadStage.NOT_REACHABLE] == "Not reacheable"


# --- payload shape ---------------------------------------------------------------------------

def test_event_shape_and_name():
    ev = build_event(_lead(mobile="9876543210"), LeadStage.RTU, event_time=0)
    assert ev["event_name"] == "RTU"
    assert ev["action_source"] == "system_generated"
    assert set(ev) == {"event_name", "event_time", "action_source", "event_id", "user_data"}


def test_accepts_stage_as_string():
    # _fire_capi may pass either the enum or its value.
    ev = build_event(_lead(mobile="9876543210"), "New Lead", event_time=0)
    assert ev["event_name"] == "New Lead"


# --- event_id dedup key ----------------------------------------------------------------------

def test_event_id_deterministic_and_format():
    lead = _lead(uid="abc", mobile="9876543210")
    a = build_event(lead, LeadStage.ENGAGED, event_time=0)
    b = build_event(lead, LeadStage.ENGAGED, event_time=0)
    assert a["event_id"] == b["event_id"], "event_id must be deterministic (it is the dedup key)"
    assert a["event_id"] == "abc:Engagged:19700101"


def test_event_id_day_tracks_event_time():
    day = time.strftime("%Y%m%d", time.gmtime(1700000000))
    ev = build_event(_lead(uid="abc", mobile="9876543210"), LeadStage.FTU, event_time=1700000000)
    assert ev["event_id"] == f"abc:FTU:{day}"


# --- PII normalization + hashing -------------------------------------------------------------

def test_phone_normalized_then_hashed():
    expected = _sha("919876543210")
    for raw in ("9876543210", "+91 98765-43210", "09876543210", "919876543210"):
        ev = build_event(_lead(mobile=raw), LeadStage.RTU, event_time=0)
        assert ev["user_data"]["ph"] == expected, f"phone {raw!r} normalized/hashed wrong"
        assert ev["user_data"]["ph"] != raw  # never send the raw number


def test_email_normalized_then_hashed():
    ev = build_event(_lead(email="  A@B.com "), LeadStage.RTU, event_time=0)
    assert ev["user_data"]["em"] == _sha("a@b.com")


def test_leadgen_id_passed_through_unhashed():
    ev = build_event(_lead(mobile="9876543210", leadgen_id="lg-42"), LeadStage.RTU, event_time=0)
    assert ev["user_data"]["lead_id"] == "lg-42"


def test_sha256_helper():
    assert _sha256("x") == _sha("x")
    assert _sha256(None) is None


# --- "nothing to match on" guards ------------------------------------------------------------

def test_no_contact_data_returns_none():
    assert build_event(_lead(), LeadStage.RTU, event_time=0) is None


def test_partial_contact_only_sends_what_it_has():
    only_phone = build_event(_lead(mobile="9876543210"), LeadStage.RTU, event_time=0)
    assert "ph" in only_phone["user_data"] and "em" not in only_phone["user_data"]
    only_email = build_event(_lead(email="a@b.com"), LeadStage.RTU, event_time=0)
    assert "em" in only_email["user_data"] and "ph" not in only_email["user_data"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all facebook_capi checks passed")
