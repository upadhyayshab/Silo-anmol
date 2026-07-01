"""Pins the IST "today" boundary used by the working queue (DB-free).

The queue drops a lead once a call is logged *today*, where "today" is the IST
calendar day. That hinges on ``_start_of_ist_day`` mapping any instant to the UTC
time of the most recent IST midnight (always 18:30 UTC the prior day, since
IST = UTC+5:30). A drift here would either resurface worked leads early or hide
unreached ones a day too long. Run::

    python tests/test_today_queue.py     # or: pytest tests/test_today_queue.py
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
# leadService pulls in managers -> SharedBackend; add it like the runtime PYTHONPATH.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

from services.leadService import _start_of_ist_day  # noqa: E402


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_afternoon_ist_maps_to_prior_1830_utc():
    # 10:00 UTC = 15:30 IST on the same day -> IST midnight = 00:00 IST = 18:30 UTC prev day.
    assert _start_of_ist_day(_utc(2026, 7, 1, 10, 0)) == _utc(2026, 6, 30, 18, 30)


def test_late_utc_that_is_next_ist_day():
    # 20:00 UTC on 07-01 = 01:30 IST on 07-02 -> IST midnight = 07-02 00:00 IST = 18:30 UTC on 07-01.
    assert _start_of_ist_day(_utc(2026, 7, 1, 20, 0)) == _utc(2026, 7, 1, 18, 30)


def test_result_is_always_1830_utc_and_tzaware():
    for h in range(0, 24):
        start = _start_of_ist_day(_utc(2026, 3, 15, h, 0))
        assert start.tzinfo is not None
        assert (start.hour, start.minute) == (18, 30)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all today_queue checks passed")
