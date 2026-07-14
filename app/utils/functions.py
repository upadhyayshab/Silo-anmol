from datetime import datetime, date, timezone
from utils.timeutils import IST

def ensure_date(dt):
    """
    Returns a date object from either a date or datetime object, shifted to
    the IST calendar day when given a datetime. If input is None, returns None.
    """
    if dt is None:
        return None
    if isinstance(dt, datetime):
        aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return aware.astimezone(IST).date()
    return dt
