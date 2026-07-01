"""Which stages a re-filled lead reopens to (DB-free).

Pins the re-engagement policy: a returning form-fill reopens a *dormant* lead
(LAPSED / NOT_REACHABLE / NOT_QUALIFIED) back to ENGAGED, and leaves an already
active/converted lead (NEW_LEAD / ENGAGED / FTU / RTU) exactly where it is.

Run::

    python tests/test_reengage.py     # or: pytest tests/test_reengage.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

from utils.crm_enums import LeadStage                       # noqa: E402
from services.leadService import _reengage_target_stage     # noqa: E402


def test_dormant_stages_reopen_to_engaged():
    for stage in (LeadStage.LAPSED, LeadStage.NOT_REACHABLE, LeadStage.NOT_QUALIFIED):
        assert _reengage_target_stage(stage) is LeadStage.ENGAGED


def test_active_and_converted_stages_stay_put():
    for stage in (LeadStage.NEW_LEAD, LeadStage.ENGAGED, LeadStage.FTU, LeadStage.RTU):
        assert _reengage_target_stage(stage) is None


def test_accepts_raw_string_value():
    # Leads may hand us the stored string value rather than the enum member.
    assert _reengage_target_stage(LeadStage.LAPSED.value) is LeadStage.ENGAGED
    assert _reengage_target_stage(LeadStage.RTU.value) is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} re-engagement unit tests passed")


if __name__ == "__main__":
    _run_all()
