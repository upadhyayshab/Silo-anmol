"""Canonical IST day-boundary helpers.

The DB stores UTC (timestamptz). The business operates in IST (UTC+5:30).
Every "today"/date-range/day-grouping computation must anchor to the IST
calendar day, then convert to UTC for the query. Import from here instead
of redefining `IST = timezone(timedelta(hours=5, minutes=30))` locally.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))


def ist_now() -> datetime:
    return datetime.now(IST)


def ist_today() -> date:
    return datetime.now(IST).date()


def ist_day_start_utc(d: Optional[date] = None) -> datetime:
    """00:00 IST of `d` (default today) as a UTC instant."""
    d = d or ist_today()
    return datetime.combine(d, time.min, tzinfo=IST).astimezone(timezone.utc)


def ist_day_bounds(from_date: Optional[date], to_date: Optional[date]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Inclusive (utc_gte, utc_lte) covering the IST day range. Neither given -> today.

    Use for "what happened today" gauges/defaults (e.g. the CRM lane's
    "assigned today"). For an optional report filter where no dates given
    should mean "no filter" (not "today only"), use ist_range_bounds instead."""
    if from_date is None and to_date is None:
        from_date = to_date = ist_today()
    return ist_range_bounds(from_date, to_date)


def ist_range_bounds(from_date: Optional[date], to_date: Optional[date]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Inclusive (utc_gte, utc_lte) covering the IST day range, no default. Either
    or both may be None (no lower/upper bound at all — not "today")."""
    gte = datetime.combine(from_date, time.min, tzinfo=IST).astimezone(timezone.utc) if from_date else None
    lte = datetime.combine(to_date, time.max, tzinfo=IST).astimezone(timezone.utc) if to_date else None
    return gte, lte


def ist_date(col):
    """SQLAlchemy expression: IST calendar date of a timestamptz column.

    Use for GROUP BY / WHERE on timestamptz columns, e.g.
    `select(ist_date(Lead.created_at), func.count())`. Do NOT apply to a
    column that is already a plain DATE (no AT TIME ZONE shift needed —
    see customer_orders.actual_delivery_date, which is `date` in prod
    despite the ORM declaring DateTime(timezone=True)).
    """
    from sqlalchemy import func
    return func.date(func.timezone('Asia/Kolkata', col))
