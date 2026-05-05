from datetime import datetime, date

def ensure_date(dt):
    """
    Returns a date object from either a date or datetime object.
    If input is None, returns None.
    """
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.date()
    return dt
