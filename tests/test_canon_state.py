"""Pins canon_state: lead/user/page state -> canonical LOWERCASE (casing + misspellings
fold to a real state; real non-state words pass through lowercased; garbage -> None).
Twin of scripts/lsq/lsq_backfill_leads.derive_state (which returns Title Case). Run::

    python tests/test_canon_state.py     # or: pytest tests/test_canon_state.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

from services.leadService import canon_state  # noqa: E402


def test_exact_casing_folds_to_lowercase():
    assert canon_state("Karnataka") == "karnataka"
    assert canon_state("karnataka") == "karnataka"
    assert canon_state("KARNATAKA") == "karnataka"
    assert canon_state("  Andhra Pradesh ") == "andhra pradesh"
    assert canon_state("PUNJAB") == "punjab"


def test_misspellings_heal():
    assert canon_state("karanataka") == "karnataka"
    assert canon_state("andra pradesh") == "andhra pradesh"
    assert canon_state("ಕರ್ನಾಟಕ") == "karnataka"


def test_telangana_aliased_to_andhra_pradesh():
    # Telangana has no telecallers; its Telugu-speaking leads route to the AP team.
    assert canon_state("Telangana") == "andhra pradesh"
    assert canon_state("telangana") == "andhra pradesh"
    assert canon_state("telanganga") == "andhra pradesh"   # misspelling + alias


def test_real_nonstate_words_pass_through_lowercased():
    # A district/city in the state field is kept (surfaced for manual mapping), lowercased.
    assert canon_state("Tumkur") == "tumkur"
    assert canon_state("Bengaluru Rural") == "bengaluru rural"


def test_garbage_becomes_none():
    assert canon_state("571439") is None      # pincode
    assert canon_state("...") is None
    assert canon_state("") is None
    assert canon_state("   ") is None
    assert canon_state(None) is None


def test_idempotent():
    for v in ("Karnataka", "andhra pradesh", "Punjab", "Tumkur"):
        once = canon_state(v)
        assert canon_state(once) == once


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all canon_state checks passed")
