"""Order event enums + fold. Run: pytest tests/test_order_events_fold.py  OR  python tests/test_order_events_fold.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from utils.constants import (
    OrderEventType, CancellationReason, EscalationState, Custody,
    NON_DELIVERED_RIDER_OUTCOMES, SETTING_AGING_CANCEL_ENABLED,
)
from services.order_events_service import fold_order_state, should_escalate, OrderState

def test_enums_have_expected_members():
    assert OrderEventType.RIDER_DISPOSITION == "RIDER_DISPOSITION"
    assert OrderEventType.ESCALATED_CRM == "ESCALATED_CRM"
    assert OrderEventType.ESCALATED_LOGISTICS == "ESCALATED_LOGISTICS"
    assert OrderEventType.DELETED == "DELETED"
    assert CancellationReason.AGED_OUT == "AGED_OUT"
    assert CancellationReason.CUSTOMER_DECLINED == "CUSTOMER_DECLINED"
    assert EscalationState.CRM_REVIEW == "CRM_REVIEW"
    assert Custody.RIDER == "RIDER"
    assert "customer_not_available" in NON_DELIVERED_RIDER_OUTCOMES
    assert "delivered" not in NON_DELIVERED_RIDER_OUTCOMES
    assert SETTING_AGING_CANCEL_ENABLED == "aging_cancel_enabled"


def _ev(t, i, status=None, payload=None):
    return SimpleNamespace(event_type=t, status_changed_to=status,
                           created_at=datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(hours=i),
                           payload=payload)

def test_fresh_order_is_outlet_zero_attempts():
    s = fold_order_state([_ev("CREATED", 0)])
    assert s.custody == "OUTLET" and s.attempt_count == 0
    assert s.escalation_state == "NONE" and not should_escalate(s)

def test_assigned_flips_custody_to_rider():
    s = fold_order_state([_ev("CREATED", 0), _ev("ASSIGNED", 1)])
    assert s.custody == "RIDER"

def test_three_non_delivered_dispositions_trigger_escalation():
    evs = [_ev("CREATED", 0), _ev("ASSIGNED", 1)]
    evs += [_ev("RIDER_DISPOSITION", 2 + i, status="customer_not_available") for i in range(3)]
    s = fold_order_state(evs)
    assert s.attempt_count == 3
    assert should_escalate(s)  # 3 >= 3*(0+1)

def test_two_attempts_do_not_trigger():
    evs = [_ev("RIDER_DISPOSITION", i, status="unable_to_contact") for i in range(2)]
    s = fold_order_state(evs)
    assert s.attempt_count == 2 and not should_escalate(s)

def test_delivered_disposition_does_not_count_and_sets_customer():
    s = fold_order_state([_ev("RIDER_DISPOSITION", 0, status="delivered")])
    assert s.attempt_count == 0 and s.custody == "CUSTOMER"

def test_already_in_crm_review_does_not_re_escalate():
    evs = [_ev("RIDER_DISPOSITION", i, status="attempted") for i in range(3)]
    evs.append(_ev("ESCALATED_CRM", 4))
    s = fold_order_state(evs)
    assert s.escalation_state == "CRM_REVIEW"
    assert not should_escalate(s)  # already escalated, don't re-fire
    assert s.escalated_at == evs[-1].created_at

def test_loop_rearms_after_logistics_bounce_back():
    evs = [_ev("RIDER_DISPOSITION", i, status="attempted") for i in range(3)]
    evs.append(_ev("ESCALATED_CRM", 4))
    evs.append(_ev("ESCALATED_LOGISTICS", 5))          # CRM confirmed -> back to logistics
    s = fold_order_state(evs)
    assert s.escalation_count == 1 and s.escalation_state == "LOGISTICS"
    assert s.custody == "OUTLET" and not should_escalate(s)  # 3 >= 3*2? no
    # three MORE attempts re-arm
    evs += [_ev("RIDER_DISPOSITION", 6 + i, status="attempted") for i in range(3)]
    s2 = fold_order_state(evs)
    assert s2.attempt_count == 6 and should_escalate(s2)  # 6 >= 3*(1+1)

def test_escalated_at_is_first_escalation_only():
    evs = [_ev("RIDER_DISPOSITION", i, status="attempted") for i in range(3)]
    evs.append(_ev("ESCALATED_CRM", 3))
    evs.append(_ev("ESCALATED_LOGISTICS", 4))
    evs += [_ev("RIDER_DISPOSITION", 5 + i, status="attempted") for i in range(3)]
    evs.append(_ev("ESCALATED_CRM", 9))                # second escalation
    s = fold_order_state(evs)
    assert s.escalated_at == evs[3].created_at         # the FIRST ESCALATED_CRM, never moved

def test_returned_to_outlet_sets_custody_outlet():
    evs = [_ev("ASSIGNED", 0), _ev("RIDER_DISPOSITION", 1, status="attempted"),
           _ev("RETURNED_TO_OUTLET", 2)]
    s = fold_order_state(evs)
    assert s.custody == "OUTLET"

def test_cancelled_carries_reason_from_payload():
    s = fold_order_state([_ev("CANCELLED", 0, payload={"cancellation_reason": "AGED_OUT"})])
    assert s.cancellation_reason == "AGED_OUT"


if __name__ == "__main__":
    test_enums_have_expected_members()
    test_fresh_order_is_outlet_zero_attempts()
    test_assigned_flips_custody_to_rider()
    test_three_non_delivered_dispositions_trigger_escalation()
    test_two_attempts_do_not_trigger()
    test_delivered_disposition_does_not_count_and_sets_customer()
    test_already_in_crm_review_does_not_re_escalate()
    test_loop_rearms_after_logistics_bounce_back()
    test_escalated_at_is_first_escalation_only()
    test_returned_to_outlet_sets_custody_outlet()
    test_cancelled_carries_reason_from_payload()
    print("PASS")
