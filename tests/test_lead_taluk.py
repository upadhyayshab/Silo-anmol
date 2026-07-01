import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import path_setup  # noqa: F401

from models import LeadCreateRequest, LeadUpdateRequest, LeadResponse
from services.leadService import EDITABLE_FIELDS, canon_geo


def test_create_request_accepts_taluk():
    req = LeadCreateRequest(first_name="A", mobile="9999999999", taluk="Tiptur")
    assert req.taluk == "Tiptur"


def test_update_request_accepts_taluk():
    assert "taluk" in LeadUpdateRequest.model_fields


def test_response_exposes_taluk():
    assert "taluk" in LeadResponse.model_fields


def test_taluk_is_editable():
    assert "taluk" in EDITABLE_FIELDS


def test_canon_geo_lowercases_and_blanks():
    assert canon_geo("  Nicobar ") == "nicobar"
    assert canon_geo("KARNATAKA") == "karnataka"
    assert canon_geo("") is None
    assert canon_geo(None) is None


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("PASS", n)
