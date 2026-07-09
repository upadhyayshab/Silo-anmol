"""Exotel /usermapping quirks, pinned as tests (all undocumented, verified 2026-07-09).

  - the bulk payload carries SipSecret -> it must never leave the adapter;
  - PUT takes a SINGLE OBJECT (an array -> 400 invalid Payload);
  - drift is compared on NORMALIZED numbers, else every softphone open PUTs
    (and rotates SipSecret) because Exotel stores '08068875144' and '+918068875144';
  - CRM email != Exotel email (DAILER_EMAIL_OVERRIDES), so the adapter translates
    at its boundary — a mismatch would show every overridden agent as "not mapped".

Run::

    python tests/test_exotel_usermapping.py    # or: pytest tests/test_exotel_usermapping.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

import adapters.exotel as E  # noqa: E402


def run(coro):
    return asyncio.run(coro)


BULK = {
    "Data": {
        "Users": [
            {"AppUserId": "a@x.com", "VirtualNumber": "+918068875144",
             "SipId": "sip:aaa", "SipSecret": "SHOULD_NOT_LEAK",
             "ExotelAccountSid": "sid1", "AppUsername": "A", "Email": "a@x.com",
             "ExotelUserName": "A", "AgentNumber": "+919000000001"},
            {"AppUserId": "b@x.com", "VirtualNumber": "08068875144",
             "SipId": "sip:bbb", "SipSecret": "ALSO_SECRET"},
        ]
    }
}

EXOPHONES = {"incoming_phone_numbers": [
    {"phone_number": "+918048332571", "friendly_name": "08048332571",
     "voice_url": "https://my.mum1.exotel.com/silofortune1m/exoml/start_voice/45195"},
    {"phone_number": "+918068452917", "friendly_name": "08068452917", "voice_url": ""},
]}


# --- bulk read ---------------------------------------------------------------------

def test_bulk_parse_strips_sipsecret():
    rows = E._usermappings_from_body(BULK)
    assert len(rows) == 2
    assert all("SipSecret" not in r for r in rows), rows
    assert rows[0]["VirtualNumber"] == "+918068875144"


def test_bulk_parse_empty_body():
    assert E._usermappings_from_body({}) == []
    assert E._usermappings_from_body({"Data": {}}) == []


# --- PUT body ----------------------------------------------------------------------

def test_put_body_is_a_single_object_not_a_list():
    # An array body returns 400 "invalid Payload" from Exotel.
    body = E._put_body(BULK["Data"]["Users"][0], "+914012345678")
    assert isinstance(body, dict), type(body)
    assert body["VirtualNumber"] == "+914012345678"
    assert body["AppUserId"] == "a@x.com"
    assert body["ExotelAccountSid"] == "sid1"
    assert "SipSecret" not in body


def test_put_body_tolerates_missing_optional_fields():
    body = E._put_body({"AppUserId": "b@x.com"}, "+9180")
    assert body["Email"] == "b@x.com" and body["AgentNumber"] == ""


# --- drift guard -------------------------------------------------------------------

def test_needs_update_false_for_equivalent_formats():
    # THE GUARD: '08068875144' and '+918068875144' are the same number.
    assert E._needs_vn_update("08068875144", "+918068875144") is False
    assert E._needs_vn_update("+918068875144", "+918068875144") is False


def test_needs_update_true_for_a_real_change():
    assert E._needs_vn_update("+918068875144", "+914012345678") is True


def test_needs_update_false_when_desired_missing():
    assert E._needs_vn_update("+918068875144", "") is False
    assert E._needs_vn_update("+918068875144", None) is False


def test_needs_update_true_when_unmapped():
    assert E._needs_vn_update(None, "+918068875144") is True


# --- exophones ---------------------------------------------------------------------

def test_exophones_with_flow_keeps_unbound_numbers():
    rows = E._exophones_with_flow(EXOPHONES)
    assert [r["number"] for r in rows] == ["+918048332571", "+918068452917"]
    assert rows[0]["flow_id"] == "45195"
    assert rows[1]["flow_id"] is None      # no voice_url -> not on any flow


# --- CRM email <-> Exotel email ------------------------------------------------------

def _adapter(**kw):
    return E.ExotelAdapter(sid="s", api_key="k", api_token="t", **kw)


def test_email_override_translation_round_trips():
    # DAILER_EMAIL_OVERRIDES maps CRM email -> Exotel email. Everything outside the
    # adapter speaks CRM emails; a mismatch would show the agent as "not mapped".
    a = _adapter(email_overrides="crm.bob@x.com:bob@exotel.com")
    assert a._to_exotel_email("CRM.Bob@x.com ") == "bob@exotel.com"
    assert a._to_crm_email("bob@exotel.com") == "crm.bob@x.com"
    # idempotent: an already-Exotel address is not a key
    assert a._to_exotel_email("bob@exotel.com") == "bob@exotel.com"
    # unknown addresses pass through, lowercased
    assert a._to_exotel_email("Zoe@x.com") == "zoe@x.com"
    assert a._to_crm_email("Zoe@x.com") == "zoe@x.com"


# --- which VirtualNumber a NEW mapping gets ------------------------------------------

class _FakeAdapter:
    """Only the bits `_choose_vn` touches."""

    def __init__(self, exophones):
        self._exophones = exophones

    async def list_caller_ids(self):
        return self._exophones


def test_choose_vn_prefers_the_configured_number():
    a = _FakeAdapter([{"number": "+918048332571"}])
    assert run(E._choose_vn(a, "+914012345678")) == "+914012345678"


def test_choose_vn_falls_back_to_first_exophone():
    # ponytail-era behaviour, kept only as a fallback when config is absent.
    a = _FakeAdapter([{"number": "+918048332571"}, {"number": "+918068875144"}])
    assert run(E._choose_vn(a, "")) == "+918048332571"


def test_choose_vn_empty_when_nothing_available():
    assert run(E._choose_vn(_FakeAdapter([]), "")) == ""


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all exotel usermapping checks passed")
