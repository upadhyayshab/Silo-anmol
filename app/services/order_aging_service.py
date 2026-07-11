"""Nightly 30-day auto-cancel for escalated, unresolved orders. Kill-switch flagged."""
from datetime import datetime, timedelta, timezone
from typing import Optional

AGE_LIMIT = timedelta(days=30)


def is_aged_out(escalated_at: Optional[datetime], *, now: Optional[datetime] = None) -> bool:
    """True when an order has been in escalation >= 30 days. None escalated_at never ages."""
    if escalated_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - escalated_at) >= AGE_LIMIT
