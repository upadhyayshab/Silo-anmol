"""Guards the call-disposition taxonomy mapping (DB-free).

The telecaller picks a 2-level disposition after each call; the backend maps the
sub-disposition to a CallOutcome and applies the Do-Not-Call effect. The sub-labels are
the contract with the frontend (leadEnums.js DISPOSITIONS) — a typo on either side would
silently fall back to "answered", so pin the exact set here. Run::

    python tests/test_disposition_mapping.py    # or: pytest tests/test_disposition_mapping.py
"""
import os
import sys

_here = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_here, "..", "app"))
# leadService pulls in managers -> SharedBackend; add it like the runtime PYTHONPATH
# (needed below to import the not_connected predicate DB-free).
sys.path.insert(0, os.path.join(_here, "..", "SharedBackend", "src"))

from utils.crm_enums import (CallOutcome, LeadStage, DISPOSITION_OUTCOME,  # noqa: E402
                             DNC_SUB_DISPOSITIONS, DISPOSITION_STAGE, PROTECTED_STAGES)
from services.leadService import _is_not_connected_call, _is_outbound_call  # noqa: E402

# Mirror of the frontend taxonomy (leadEnums.js DISPOSITIONS) — keep in sync.
EXPECTED_SUBS = {
    "Ringing No Response", "Busy", "Switched off", "Not Reachable/Out of Coverage", "Call dropped",
    "Call Back", "Order Booked", "Interested", "Not Interested", "Do Not Call", "Wrong Number",
    "Invalid Number",
    "Max Call Attempts (20 calls)", "Farmer - Just Browsing", "Non-Farmer - Random Visitor",
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


# ---- Task A: "Max Call Done" + two new dead-end Connected subs ------------------------

def test_new_subs_map_to_expected_stages():
    assert DISPOSITION_STAGE["Farmer - Just Browsing"] == LeadStage.NOT_QUALIFIED
    assert DISPOSITION_STAGE["Non-Farmer - Random Visitor"] == LeadStage.NOT_QUALIFIED
    assert DISPOSITION_STAGE["Max Call Attempts (20 calls)"] == LeadStage.MAX_CALL_DONE


def test_max_call_done_stage_value():
    assert LeadStage.MAX_CALL_DONE.value == "Max Call Done"


def test_max_call_done_not_protected():
    # Req 4: stays re-workable, never demotion-guarded like FTU/RTU.
    assert LeadStage.MAX_CALL_DONE.value not in PROTECTED_STAGES


def test_not_connected_predicate_counts_correctly():
    # 20 Not-Connected CALL_LOG activities + a few Connected ones -> predicate picks exactly 20.
    not_connected_activities = [
        {"details": {"disposition": "Not Connected", "sub_disposition": "Ringing No Response"}, "outcome": "not_answered"}
        for _ in range(20)
    ]
    connected_activities = [
        {"details": {"disposition": "Connected", "sub_disposition": "Interested"}, "outcome": "answered"},
        {"details": {"disposition": "Connected", "sub_disposition": "Not Interested"}, "outcome": "answered"},
        {"details": {"disposition": "Connected", "sub_disposition": "Call Back"}, "outcome": "call_back_later"},
    ]
    activities = not_connected_activities + connected_activities
    count = sum(1 for a in activities if _is_not_connected_call(a["details"], a["outcome"]))
    assert count == 20


def test_not_connected_predicate_true_for_max_call_attempts_pick():
    # The new "Max Call Attempts (20 calls)" sub itself is filed under "Not Connected".
    details = {"disposition": "Not Connected", "sub_disposition": "Max Call Attempts (20 calls)"}
    assert _is_not_connected_call(details, DISPOSITION_OUTCOME.get("Max Call Attempts (20 calls)")) is True


def test_not_connected_predicate_false_for_connected():
    details = {"disposition": "Connected", "sub_disposition": "Farmer - Just Browsing"}
    assert _is_not_connected_call(details, DISPOSITION_OUTCOME.get("Farmer - Just Browsing")) is False


# ---- Task G: outbound call count (lead detail page) ------------------------------------

def test_outbound_predicate_counts_correctly():
    # 3 outbound, 2 inbound, 1 with no direction at all -> predicate picks exactly 3.
    activities = (
        [{"details": {"direction": "outbound"}} for _ in range(3)]
        + [{"details": {"direction": "inbound"}} for _ in range(2)]
        + [{"details": {}}]
    )
    count = sum(1 for a in activities if _is_outbound_call(a["details"]))
    assert count == 3


def test_outbound_predicate_false_for_inbound():
    assert _is_outbound_call({"direction": "inbound"}) is False


def test_outbound_predicate_false_for_missing_direction():
    assert _is_outbound_call({}) is False
    assert _is_outbound_call(None) is False


def test_outbound_predicate_true_for_outbound():
    assert _is_outbound_call({"direction": "outbound"}) is True


if __name__ == "__main__":
    test_every_sub_is_mapped()
    test_all_outcomes_valid()
    test_do_not_call_flags_lead()
    test_every_sub_maps_to_a_stage()
    test_all_stages_valid()
    test_new_subs_map_to_expected_stages()
    test_max_call_done_stage_value()
    test_max_call_done_not_protected()
    test_not_connected_predicate_counts_correctly()
    test_not_connected_predicate_true_for_max_call_attempts_pick()
    test_not_connected_predicate_false_for_connected()
    test_outbound_predicate_counts_correctly()
    test_outbound_predicate_false_for_inbound()
    test_outbound_predicate_false_for_missing_direction()
    test_outbound_predicate_true_for_outbound()
    print("OK")
