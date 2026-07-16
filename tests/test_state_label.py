"""_state_label (crmReportService.py) — the "Prospect Pivot" state column label.
Routes the raw lead/order state through the existing canon_state normalizer (casing +
misspellings only, apply_alias=False) before Title-Casing, so dirty legacy rows collapse
into the real state instead of spawning duplicate pivot columns — but Telangana stays
its OWN column, since _state_label also feeds _order_state_aggregate's ORDER/REVENUE
rows and Telangana is real, distinct revenue (I1 fix: was folding Telangana's revenue
into Andhra Pradesh via apply_alias's default True). Garbage/empty/None -> "Unknown"
(never a blank column). Run::

    python tests/test_state_label.py     # or: pytest tests/test_state_label.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
import path_setup  # noqa: F401,E402  — must precede manager imports

from services.crmReportService import _state_label  # noqa: E402


def test_telangana_kept_separate_from_andhra_pradesh():
    assert _state_label("telangana") == "Telangana"
    assert _state_label("Telangana") == "Telangana"


def test_misspelling_heals_to_canonical_state():
    assert _state_label("karnatka") == "Karnataka"


def test_casing_folds_to_canonical_title_case():
    assert _state_label("KARNATAKA") == "Karnataka"
    assert _state_label("karnataka") == "Karnataka"


def test_garbage_and_empty_become_unknown():
    assert _state_label("123456") == "Unknown"   # pincode
    assert _state_label("") == "Unknown"
    assert _state_label(None) == "Unknown"
    assert _state_label("...") == "Unknown"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    print("all pass" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
