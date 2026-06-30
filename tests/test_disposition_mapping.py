"""Guards the call-disposition taxonomy mapping (DB-free).

The telecaller picks a 2-level disposition after each call; the backend maps the
sub-disposition to a CallOutcome and applies the Do-Not-Call effect. The sub-labels are
the contract with the frontend (leadEnums.js DISPOSITIONS) — a typo on either side would
silently fall back to "answered", so pin the exact set here. Run::

    python tests/test_disposition_mapping.py    # or: pytest tests/test_disposition_mapping.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from utils.crm_enums import CallOutcome, DISPOSITION_OUTCOME, DNC_SUB_DISPOSITIONS  # noqa: E402

# Mirror of the frontend taxonomy (leadEnums.js DISPOSITIONS) — keep in sync.
EXPECTED_SUBS = {
    "Ringing No Response", "Busy", "Switched off", "Not Reachable/Out of Coverage", "Call dropped",
    "Call Back", "Order Booked", "Interested Call Back", "Not Interested", "Do Not Call", "Invalid Number",
}
_VALID_OUTCOMES = {o.value for o in CallOutcome}


def test_every_sub_is_mapped():
    assert set(DISPOSITION_OUTCOME) == EXPECTED_SUBS, "backend mapping drifted from the frontend sub-dispositions"


def test_all_outcomes_valid():
    for sub, outcome in DISPOSITION_OUTCOME.items():
        assert outcome in _VALID_OUTCOMES, f"{sub!r} maps to non-CallOutcome {outcome!r}"


def test_do_not_call_flags_lead():
    assert "Do Not Call" in DNC_SUB_DISPOSITIONS
    assert DNC_SUB_DISPOSITIONS <= EXPECTED_SUBS


if __name__ == "__main__":
    test_every_sub_is_mapped()
    test_all_outcomes_valid()
    test_do_not_call_flags_lead()
    print("OK")
