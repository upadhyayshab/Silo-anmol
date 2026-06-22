"""Unit tests for the pure lead-dedup helpers (checkpoint 2.3).

These exercise the DB-free functions only, so they run without the app's
config/DB stack. Run with::

    python tests/test_dedup_utils.py
    # or, if pytest is installed:
    pytest tests/test_dedup_utils.py
"""
import os
import sys

# Make `app/` importable (utils.dedup_utils) without booting the full app.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from utils.dedup_utils import (  # noqa: E402
    normalize_mobile, normalize_email, mobile_candidates, compute_backfill,
)


def test_normalize_mobile_variants():
    assert normalize_mobile("+91 98765-43210") == "9876543210"
    assert normalize_mobile("098765 43210") == "9876543210"
    assert normalize_mobile("9876543210") == "9876543210"
    assert normalize_mobile("91 9876543210") == "9876543210"
    assert normalize_mobile("  ") is None
    assert normalize_mobile(None) is None
    # A genuinely different number must NOT collapse to the same value.
    assert normalize_mobile("9123456789") != normalize_mobile("9876543210")


def test_normalize_email():
    assert normalize_email("  Foo@Bar.COM ") == "foo@bar.com"
    assert normalize_email("") is None
    assert normalize_email(None) is None


def test_mobile_candidates_cover_stored_forms():
    cands = mobile_candidates("+91-9876543210")
    assert "9876543210" in cands       # bare national number
    assert "919876543210" in cands     # 91 country prefix
    assert "09876543210" in cands      # 0 trunk prefix
    # Digit-only: the stored column is digit-stripped before comparison, so no
    # '+'-prefixed form is needed (or wanted).
    assert all("+" not in c for c in cands)
    # No duplicates.
    assert len(cands) == len(set(cands))
    assert mobile_candidates(None) == []


def test_compute_backfill_only_fills_empty():
    existing = {"first_name": "Ramesh", "last_name": None, "email": "", "city": "Hassan"}
    incoming = {"first_name": "OVERWRITE", "last_name": "Gowda",
                "email": "ramesh@example.com", "city": "Mysuru"}
    updates = compute_backfill(existing, incoming)
    # Populated fields are preserved.
    assert "first_name" not in updates
    assert "city" not in updates
    # Empty / None fields are backfilled.
    assert updates["last_name"] == "Gowda"
    assert updates["email"] == "ramesh@example.com"


def test_compute_backfill_merges_json_gaps_only():
    existing = {"custom_fields": {"breed": "Gir"}}
    incoming = {"custom_fields": {"breed": "OVERWRITE", "lactating": True}}
    updates = compute_backfill(existing, incoming)
    assert updates["custom_fields"]["breed"] == "Gir"      # not overwritten
    assert updates["custom_fields"]["lactating"] is True   # gap filled


def test_compute_backfill_noop_when_nothing_to_fill():
    existing = {"first_name": "Ramesh", "city": "Hassan"}
    incoming = {"first_name": "Ramesh", "city": "Hassan"}
    assert compute_backfill(existing, incoming) == {}


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} dedup unit tests passed")


if __name__ == "__main__":
    _run_all()
