"""Order event enums + fold. Run: pytest tests/test_order_events_fold.py  OR  python tests/test_order_events_fold.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa

from utils.constants import (
    OrderEventType, CancellationReason, EscalationState, Custody,
    NON_DELIVERED_RIDER_OUTCOMES, SETTING_AGING_CANCEL_ENABLED,
)

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


if __name__ == "__main__":
    test_enums_have_expected_members()
    print("PASS")
