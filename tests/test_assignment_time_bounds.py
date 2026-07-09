"""Pins the tz-awareness of the assignment time bounds (DB-free).

`created_at` is a timestamptz on the SharedBackend base schema, so the driver hands back
tz-AWARE datetimes. Returning a NAIVE bound from these helpers crashed the 5-minute
sweep_unassigned job the moment a caller compared the bound in Python instead of in SQL:

    File "app/services/leadService.py", line 840, in _counts
      if l.created_at and l.created_at >= today_start:
    TypeError: can't compare offset-naive and offset-aware datetimes

A "simplify" that re-adds `.replace(tzinfo=None)` to either helper brings the crash back
(and quietly makes the SQL bounds depend on the DB session's TimeZone). Run::

    python tests/test_assignment_time_bounds.py   # or: pytest tests/test_assignment_time_bounds.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

_here = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_here, "..", "app"))
# assignmentService pulls in managers -> SharedBackend; add it like the runtime PYTHONPATH.
sys.path.insert(0, os.path.join(_here, "..", "SharedBackend", "src"))

from services.assignmentService import (  # noqa: E402
    IST, CALL_ACCESS_TTL, _ist_day_start_utc, _ttl_cutoff,
)


def _aware_utc(dt) -> bool:
    return dt.tzinfo is not None and dt.utcoffset() == timedelta(0)


def test_day_start_is_aware_utc():
    assert _aware_utc(_ist_day_start_utc())


def test_ttl_cutoff_is_aware_utc():
    assert _aware_utc(_ttl_cutoff())


def test_bounds_compare_against_an_aware_created_at():
    """The exact comparison that crashed sweep_unassigned (distribute_leads._counts).

    asyncpg returns aware datetimes for timestamptz; comparing must not raise TypeError.
    """
    created_at = datetime.now(timezone.utc)
    assert isinstance(created_at >= _ist_day_start_utc(), bool)
    assert isinstance(created_at >= _ttl_cutoff(), bool)


def test_day_start_is_todays_ist_midnight():
    day_start = _ist_day_start_utc()
    ist = day_start.astimezone(IST)
    assert (ist.hour, ist.minute, ist.second, ist.microsecond) == (0, 0, 0, 0)
    # It is *today's* IST midnight: in the past, and less than a day ago.
    elapsed = datetime.now(timezone.utc) - day_start
    assert timedelta(0) <= elapsed < timedelta(days=1)


def test_ttl_cutoff_is_one_ttl_behind_now():
    behind = datetime.now(timezone.utc) - _ttl_cutoff()
    assert CALL_ACCESS_TTL <= behind < CALL_ACCESS_TTL + timedelta(seconds=10)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all green")
