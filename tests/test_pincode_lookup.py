import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import path_setup  # noqa: F401

from routers.v1.pincodes import _tc, _dedupe, _from_pypinindia


def test_tc_normalizes_and_blanks():
    assert _tc("  KARNATAKA ") == "Karnataka"
    assert _tc("") is None
    assert _tc(None) is None


def test_dedupe_keeps_first_seen_order():
    opts = [("Karnataka", "Tumkur", "Tiptur"),
            ("Karnataka", "Tumkur", "Tiptur"),
            ("Karnataka", "Tumkur", "Gubbi")]
    assert _dedupe(opts) == [("Karnataka", "Tumkur", "Tiptur"),
                             ("Karnataka", "Tumkur", "Gubbi")]


def test_from_pypinindia_extracts_dedupes_titlecases():
    raw = [
        {"statename": "KARNATAKA", "districtname": "TUMKUR", "taluk": "TIPTUR"},
        {"statename": "KARNATAKA", "district": "TUMKUR", "taluk": "TIPTUR"},   # dup via alt key
        {"statename": "KARNATAKA", "districtname": "TUMKUR", "taluk": "GUBBI"},
    ]
    assert _from_pypinindia(raw) == [("Karnataka", "Tumkur", "Tiptur"),
                                     ("Karnataka", "Tumkur", "Gubbi")]


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("PASS", n)
