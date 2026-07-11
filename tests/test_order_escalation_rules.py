"""30-day aging boundary. Run: python tests/test_order_escalation_rules.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
import path_setup  # noqa
from datetime import datetime, timezone, timedelta
from services.order_aging_service import is_aged_out

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)

def test_day_29_not_aged():
    assert is_aged_out(NOW - timedelta(days=29), now=NOW) is False

def test_day_31_aged():
    assert is_aged_out(NOW - timedelta(days=31), now=NOW) is True

def test_exactly_30_days_aged():
    assert is_aged_out(NOW - timedelta(days=30), now=NOW) is True

def test_none_escalated_at_never_ages():
    assert is_aged_out(None, now=NOW) is False


if __name__ == "__main__":
    test_day_29_not_aged()
    test_day_31_aged()
    test_exactly_30_days_aged()
    test_none_escalated_at_never_ages()
    print("PASS")
