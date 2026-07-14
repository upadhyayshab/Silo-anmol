"""Attendance / billing dashboard.

Durable per-telecaller-per-IST-day working span. `record_seen` upserts today's row from
every attendance signal (presence heartbeat + login + logout) using LEAST/GREATEST, so
first_seen/last_seen bound the day even when an agent just closes the tab (the ~20s
heartbeat keeps last_seen fresh; login/logout extend the envelope at the edges — the
"later of the two" rule). `month_overview` rolls the rows up for the dashboard.

The span math (`day_hours`, `is_present`) is pure so it is unit-tested DB-free, matching
the rest of the CRM suite.
"""
import calendar
import logging
import uuid
from datetime import datetime, timezone, timedelta, date
from typing import Optional, List, Dict, Any

import sqlalchemy as db

from managers import AttendanceDayManager, AttendanceDaySchema, UserSchema
from utils.constants import OWNER_ROLES
from utils.timeutils import IST

logger = logging.getLogger(__name__)

# A day counts as Present when the first-in -> last-out span reaches this many hours.
PRESENT_HOURS = 7.0


def day_hours(first_seen: datetime, last_seen: datetime) -> float:
    """Worked hours = last_seen - first_seen (span; breaks included), never negative.

    Rounded to 2 dp (not 1): at 1 dp a 6h59m span rounds to 7.0 and would read Present,
    over-crediting the billing boundary. 2 dp keeps display and the >=7h status honest."""
    return round(max(0.0, (last_seen - first_seen).total_seconds() / 3600.0), 2)


def is_present(hours: float) -> bool:
    return hours >= PRESENT_HOURS


def _role_value(role):
    return role.value if hasattr(role, "value") else role


def _month_bounds(year: int, month: int):
    """Inclusive first/last IST calendar dates of the month."""
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


async def record_seen(engine, user_id: str, ts: Optional[datetime] = None) -> None:
    """Fold one attendance signal into today's (IST) row: first_seen = LEAST(existing, ts),
    last_seen = GREATEST(existing, ts). Best-effort — callers must never let this block the
    heartbeat/login it rides on (see the try/except at call sites)."""
    ts = ts or datetime.now(timezone.utc)
    work_date = ts.astimezone(IST).date()

    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
        set_ = {
            "first_seen": db.func.least(AttendanceDaySchema.first_seen, ts),
            "last_seen": db.func.greatest(AttendanceDaySchema.last_seen, ts),
        }
    else:
        # No-DB fallback (sqlite) has no LEAST/GREATEST. ts is "now", so it only ever
        # extends last_seen; first_seen is left as first set. Prod is always postgres.
        from sqlalchemy.dialects.sqlite import insert as _insert
        set_ = {"last_seen": ts}

    stmt = _insert(AttendanceDaySchema).values(
        uid=f"attendance_day_{uuid.uuid4()}",
        user_id=user_id, work_date=work_date, first_seen=ts, last_seen=ts,
    ).on_conflict_do_update(index_elements=["user_id", "work_date"], set_=set_)

    mgr = AttendanceDayManager(engine)
    async with mgr.session_factory() as session:
        await session.execute(stmt)
        await session.commit()


async def record_seen_safe(engine, user_id: str, ts: Optional[datetime] = None) -> None:
    """record_seen wrapped so attendance can never break the heartbeat/login/logout path."""
    try:
        await record_seen(engine, user_id, ts)
    except Exception as e:
        logger.warning("attendance: could not record for %s: %s", user_id, e)


async def month_overview(engine, *, year: int, month: int, scope_owner_id=None, agency_id=None) -> Dict[str, Any]:
    """Per-telecaller attendance for the month: each day's span/hours/present + a summary.

    Scoped to telecaller roles (OWNER_ROLES). `scope_owner_id` None = all (superadmin), a
    list = agency roster, a single id = one agent. Agents with no rows this month still
    appear (0 days present) so an all-absent agent is visible for billing."""
    first, last = _month_bounds(year, month)
    role_vals = {_role_value(r) for r in OWNER_ROLES}

    mgr = AttendanceDayManager(engine)
    async with mgr.session_factory() as session:
        uq = db.select(UserSchema.uid, UserSchema.full_name, UserSchema.email, UserSchema.role) \
               .where(UserSchema.is_active.is_(True))
        if isinstance(scope_owner_id, (list, tuple, set)):
            uq = uq.where(UserSchema.uid.in_(list(scope_owner_id)))
        elif scope_owner_id:
            uq = uq.where(UserSchema.uid == scope_owner_id)
        if agency_id:
            uq = uq.where(UserSchema.agency_id == agency_id)
        users = [(uid, name or email or uid)
                 for uid, name, email, role in (await session.execute(uq)).all()
                 if _role_value(role) in role_vals]
        uids = [u[0] for u in users]

        rows_by_user: Dict[str, List[AttendanceDaySchema]] = {}
        if uids:
            arows = (await session.execute(
                db.select(AttendanceDaySchema).where(
                    AttendanceDaySchema.user_id.in_(uids),
                    AttendanceDaySchema.work_date >= first,
                    AttendanceDaySchema.work_date <= last,
                    AttendanceDaySchema.deleted_at.is_(None),
                ).order_by(AttendanceDaySchema.work_date)
            )).scalars().all()
            for a in arows:
                rows_by_user.setdefault(a.user_id, []).append(a)

    agents = []
    for uid, name in sorted(users, key=lambda u: u[1].lower()):
        days, total, present_days = [], 0.0, 0
        for a in rows_by_user.get(uid, []):
            h = day_hours(a.first_seen, a.last_seen)
            p = is_present(h)
            total += h
            present_days += 1 if p else 0
            days.append({
                "date": a.work_date.isoformat(), "hours": h, "present": p,
                "first_seen": a.first_seen.isoformat(), "last_seen": a.last_seen.isoformat(),
            })
        agents.append({"user_id": uid, "name": name, "days": days,
                       "days_present": present_days, "total_hours": round(total, 1)})

    return {"month": f"{year:04d}-{month:02d}", "days_in_month": last.day,
            "present_hours": PRESENT_HOURS, "agents": agents}
