"""Pins _day_bounds: a picked calendar day is an IST day, mapped to the correct
UTC window (data is stored UTC). DB-free — just calls the pure helper.

    python tests/test_day_bounds_ist.py     # or: pytest tests/test_day_bounds_ist.py
"""
import os
import sys
from datetime import date, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

from services.crmReportService import _day_bounds  # noqa: E402


def test_ist_day_bounds_map_to_utc_window():
    gte, lte = _day_bounds(date(2026, 7, 4), date(2026, 7, 4))
    # IST midnight Jul 4 == 18:30 UTC Jul 3.
    assert gte.astimezone(timezone.utc).replace(microsecond=0).isoformat() == "2026-07-03T18:30:00+00:00"
    # IST end-of-day Jul 4 == 18:29:59(.999999) UTC Jul 4 — so a lead at 23:57 UTC Jul 4
    # (05:27 IST Jul 5) falls OUTSIDE a "to Jul 4" filter, which was the bug.
    u = lte.astimezone(timezone.utc)
    assert (u.year, u.month, u.day, u.hour, u.minute, u.second) == (2026, 7, 4, 18, 29, 59)


def test_none_passthrough():
    assert _day_bounds(None, None) == (None, None)


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
            passed += 1
    print(f"all {passed} day_bounds checks passed")
