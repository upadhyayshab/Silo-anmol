"""Pins the disposition auto-stage guards (DB-free).

The deliberate call: an ENGAGED lead is never auto-demoted to NOT_REACHABLE. Someone
already talked to this lead, so a later unanswered call ("Ringing No Response", "Busy",
"Switched off", ...) must not bury it in the Not Reachable bucket. It stays disqualifiable
(NOT_QUALIFIED) and still converts on real orders (ENGAGED -> FTU -> RTU, via
handle_post_order, which does not consult this guard).

A "simplify" that drops the ENGAGED check would silently resume demoting engaged leads on
the first missed call. Run::

    python tests/test_engaged_stage_guard.py     # or: pytest tests/test_engaged_stage_guard.py
"""
import os
import sys

_here = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_here, "..", "app"))
# leadService pulls in managers -> SharedBackend; add it like the runtime PYTHONPATH.
sys.path.insert(0, os.path.join(_here, "..", "SharedBackend", "src"))

from services.leadService import _may_auto_stage  # noqa: E402
from utils.crm_enums import DISPOSITION_STAGE, LeadStage  # noqa: E402

ENGAGED = LeadStage.ENGAGED.value

# Every sub-disposition an unanswered call can carry (all map to NOT_REACHABLE).
NOT_CONNECTED = [
    "Ringing No Response", "Busy", "Switched off",
    "Not Reachable/Out of Coverage", "Call dropped",
]


def test_engaged_never_demotes_to_not_reachable():
    for sub in NOT_CONNECTED:
        assert DISPOSITION_STAGE[sub] is LeadStage.NOT_REACHABLE, sub  # premise
        assert _may_auto_stage(ENGAGED, LeadStage.NOT_REACHABLE) is False, sub


def test_engaged_still_disqualifies():
    for sub in ("Not Interested", "Do Not Call", "Wrong Number", "Invalid Number"):
        assert DISPOSITION_STAGE[sub] is LeadStage.NOT_QUALIFIED, sub
        assert _may_auto_stage(ENGAGED, LeadStage.NOT_QUALIFIED) is True, sub


def test_untouched_lead_still_goes_not_reachable():
    # The guard is ENGAGED-only: a New Lead that doesn't pick up still lands in Not Reachable.
    assert _may_auto_stage(LeadStage.NEW_LEAD.value, LeadStage.NOT_REACHABLE) is True


def test_converted_leads_never_auto_demote():
    for stage in (LeadStage.FTU, LeadStage.RTU):
        assert _may_auto_stage(stage.value, LeadStage.NOT_REACHABLE) is False
        assert _may_auto_stage(stage.value, LeadStage.NOT_QUALIFIED) is False


def test_no_op_move_is_not_a_change():
    assert _may_auto_stage(ENGAGED, LeadStage.ENGAGED) is False
    assert _may_auto_stage(LeadStage.NOT_REACHABLE.value, LeadStage.NOT_REACHABLE) is False


def test_not_reachable_can_still_reengage():
    # "Interested"/"Call Back" on a Not Reachable lead pulls it back to Engaged.
    assert _may_auto_stage(LeadStage.NOT_REACHABLE.value, LeadStage.ENGAGED) is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all green")
