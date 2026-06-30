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

from utils.crm_enums import (CallOutcome, LeadStage, DISPOSITION_OUTCOME,  # noqa: E402
                             DNC_SUB_DISPOSITIONS, DISPOSITION_STAGE, PROTECTED_STAGES)

# Mirror of the frontend taxonomy (leadEnums.js DISPOSITIONS) — keep in sync.
EXPECTED_SUBS = {
    "Ringing No Response", "Busy", "Switched off", "Not Reachable/Out of Coverage", "Call dropped",
    "Call Back", "Order Booked", "Interested", "Not Interested", "Do Not Call", "Wrong Number",
    "Invalid Number",
}
_VALID_OUTCOMES = {o.value for o in CallOutcome}
_VALID_STAGES = {s.value for s in LeadStage}


def test_every_sub_is_mapped():
    assert set(DISPOSITION_OUTCOME) == EXPECTED_SUBS, "backend mapping drifted from the frontend sub-dispositions"


def test_all_outcomes_valid():
    for sub, outcome in DISPOSITION_OUTCOME.items():
        assert outcome in _VALID_OUTCOMES, f"{sub!r} maps to non-CallOutcome {outcome!r}"


def test_do_not_call_flags_lead():
    assert "Do Not Call" in DNC_SUB_DISPOSITIONS
    assert DNC_SUB_DISPOSITIONS <= EXPECTED_SUBS


def test_every_sub_maps_to_a_stage():
    # Every sub auto-advances the stage EXCEPT "Order Booked" (stage comes from real order placement).
    assert set(DISPOSITION_STAGE) == EXPECTED_SUBS - {"Order Booked"}, "stage map drifted from subs"


def test_all_stages_valid():
    for sub, stage in DISPOSITION_STAGE.items():
        assert stage.value in _VALID_STAGES, f"{sub!r} maps to non-LeadStage {stage!r}"
    assert PROTECTED_STAGES <= _VALID_STAGES, "PROTECTED_STAGES has an unknown stage"


if __name__ == "__main__":
    test_every_sub_is_mapped()
    test_all_outcomes_valid()
    test_do_not_call_flags_lead()
    test_every_sub_maps_to_a_stage()
    test_all_stages_valid()
    print("OK")
