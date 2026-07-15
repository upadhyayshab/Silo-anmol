"""Attendance span math (DB-free).

Pins the billing-sensitive rules:
  - worked hours = last_seen - first_seen (SPAN — a lunch break counts, matching
    "first login to last logout")
  - Present when the span is >= 7h; 6h59m must NOT round up to Present
  - IST calendar-day bucketing, month bounds

Run::
    python tests/test_attendance.py     # or: pytest tests/test_attendance.py
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta, date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

from services.attendanceService import (  # noqa: E402
    day_hours, is_present, _month_bounds, PRESENT_HOURS, IST, month_overview,
)

U = timezone.utc
F = datetime(2026, 7, 1, 9, 0, tzinfo=U)


def test_exact_seven_hours_is_present():
    assert day_hours(F, F + timedelta(hours=7)) == 7.0
    assert is_present(day_hours(F, F + timedelta(hours=7)))


def test_six_fiftynine_is_not_present():
    # The reason day_hours rounds to 2dp not 1: at 1dp this would be 7.0 -> wrongly Present.
    h = day_hours(F, F + timedelta(hours=6, minutes=59))
    assert h == 6.98 and not is_present(h)


def test_span_counts_breaks():
    # first-in -> last-out; a 2h gap in the middle is still paid span.
    assert day_hours(F, F + timedelta(hours=9)) == 9.0


def test_never_negative_on_clock_skew():
    assert day_hours(F, F - timedelta(hours=1)) == 0.0


def test_present_threshold_value():
    assert PRESENT_HOURS == 7.0
    assert is_present(7.0) and is_present(7.01) and not is_present(6.99)


def test_ist_day_bucketing():
    # 23:30 UTC is 05:00 IST the NEXT day -> belongs to that IST date.
    assert datetime(2026, 7, 1, 23, 30, tzinfo=U).astimezone(IST).date() == date(2026, 7, 2)


def test_month_bounds():
    assert _month_bounds(2026, 7) == (date(2026, 7, 1), date(2026, 7, 31))
    assert _month_bounds(2026, 2)[1].day == 28          # non-leap
    assert _month_bounds(2024, 2)[1].day == 29          # leap


class TestMonthOverviewRoleFilter(unittest.IsolatedAsyncioTestCase):
    """month_overview is the CRM Attendance tab's roster query. It must show telecallers
    only — AGENCY_ADMIN is in OWNER_ROLES (a lead-ownership concept) but agency admins
    don't clock in/out, so an AGENCY_ADMIN row would be a billing-page bug, not just noise.
    DB-backed (sqlite in-memory), same harness as tests/test_lead_fb_page_filter.py."""

    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import create_async_engine
        from managers import UserManager

        # Fresh in-memory sqlite engine per test -- same flavor the rest of the
        # suite runs against (DB_HOST unset => sqlite+aiosqlite per app/config.py).
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.users = UserManager(self.engine)
        await self.users.init_db()

    async def _seed_user(self, *, role, full_name, email):
        from managers import UserSchema
        await self.users.create(UserSchema(
            email=email, full_name=full_name, role=role, is_active=True,
        ))

    async def test_agency_admin_excluded_telecaller_included(self):
        from utils.constants import UserRole

        await self._seed_user(role=UserRole.AGENCY_ADMIN.value,
                              full_name="Agency Admin", email="agency-admin@test.com")
        await self._seed_user(role=UserRole.TELECALLER.value,
                              full_name="Telecaller One", email="telecaller-one@test.com")

        result = await month_overview(self.engine, year=2026, month=7)
        names = {a["name"] for a in result["agents"]}

        self.assertIn("Telecaller One", names)
        self.assertNotIn("Agency Admin", names)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all attendance checks passed")
    unittest.main()
