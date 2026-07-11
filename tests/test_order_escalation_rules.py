"""30-day aging boundary. Run: python tests/test_order_escalation_rules.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))
import path_setup  # noqa
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
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


async def _acoro(v):
    return v


def test_job_noop_when_flag_off():
    import services.order_aging_service as A
    calls = {"updates": 0}

    async def _fetch_all(**kw):
        return SimpleNamespace(items=[], count=0)

    async def _update(*a, **k):
        calls["updates"] += 1

    A.order_manager = SimpleNamespace(fetch_all=_fetch_all, update=_update)
    A.settings_manager = SimpleNamespace(
        get_map=lambda keys: _acoro({A_KEY: False}))
    # flag off -> job returns without scanning
    asyncio.run(A.cancel_aged_orders())
    assert calls["updates"] == 0


from utils.constants import SETTING_AGING_CANCEL_ENABLED as A_KEY


if __name__ == "__main__":
    test_day_29_not_aged()
    test_day_31_aged()
    test_exactly_30_days_aged()
    test_none_escalated_at_never_ages()
    test_job_noop_when_flag_off()
    print("PASS")
