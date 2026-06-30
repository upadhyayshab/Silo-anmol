"""Pins which heartbeat statuses count as 'logged in' for lead-assignment gating (DB-free).

new leads only auto-assign while a telecaller is present. The deliberate call here is
that a telecaller mid-call (status 'on_call') is still on shift and assignable — so
present_ids is BROADER than available_ids (inbound routing, 'available' only). A silent
"simplify" back to available-only would stop dealing leads to busy-but-working agents.
Run::

    python tests/test_presence_eligibility.py     # or: pytest tests/test_presence_eligibility.py
"""
import os
import sys

_here = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_here, "..", "app"))
# presenceService pulls in managers -> SharedBackend; add it like the runtime PYTHONPATH.
sys.path.insert(0, os.path.join(_here, "..", "SharedBackend", "src"))

from services.presenceService import PRESENT_STATUSES  # noqa: E402


def test_on_call_and_available_both_present():
    assert "available" in PRESENT_STATUSES
    assert "on_call" in PRESENT_STATUSES   # mid-call still counts as logged in


def test_offline_is_not_present():
    assert "offline" not in PRESENT_STATUSES


def test_broader_than_inbound_available_only():
    # Inbound routing keys on 'available' alone; assignment must also accept on_call.
    assert set(PRESENT_STATUSES) > {"available"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all present-eligibility checks passed")
