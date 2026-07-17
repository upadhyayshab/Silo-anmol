"""_state_label (crmReportService.py) — the "Prospect Pivot" REGION column label.
Routes the raw lead/order state through the existing canon_state normalizer (casing +
misspellings only, apply_alias=False) so dirty legacy rows heal to the real state, then
maps that state to its REGION display label via tracker_queries.REGIONS (the single
source of truth for region membership, inverted in crmReportService — no second copy of
the state lists here). Karnataka / Punjab keep their own column; Andhra Pradesh AND
Telangana fold into ONE "AP & Telangana" column (2026-07-16 stakeholder ask — an
explicit, labelled region grouping, distinct from the old silent AP-fold this module
elsewhere warns against: canon_state(..., apply_alias=False) still keeps them as
separate STATES for every other consumer, this is a display-only regroup). Any other
real state (Haryana, UP, Maharashtra, TN, Rajasthan, Uttarakhand, ...) or unrecognized-
but-real value (a district name) -> "Others". Garbage/empty/None -> "Unknown" (never a
blank column). Run::

    python tests/test_state_label.py     # or: pytest tests/test_state_label.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
import path_setup  # noqa: F401,E402  — must precede manager imports

from services.crmReportService import _state_label  # noqa: E402


def test_telangana_and_andhra_pradesh_fold_into_one_region_column():
    assert _state_label("telangana") == "AP & Telangana"
    assert _state_label("Telangana") == "AP & Telangana"
    assert _state_label("andhra pradesh") == "AP & Telangana"
    assert _state_label("Andhra Pradesh") == "AP & Telangana"


def test_karnataka_and_punjab_keep_their_own_region_column():
    assert _state_label("karnataka") == "Karnataka"
    assert _state_label("punjab") == "Punjab"


def test_misspelling_heals_to_canonical_state_then_region():
    assert _state_label("karnatka") == "Karnataka"


def test_casing_folds_to_canonical_region_label():
    assert _state_label("KARNATAKA") == "Karnataka"
    assert _state_label("karnataka") == "Karnataka"


def test_state_outside_any_region_becomes_others():
    assert _state_label("haryana") == "Others"
    assert _state_label("Maharashtra") == "Others"
    assert _state_label("tumkur") == "Others"   # district name, real word, no region


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
