"""CRM Attendance / billing dashboard.

Per-telecaller monthly attendance derived from attendance_days (first-in -> last-out span
per IST day; Present when >= 7h). Scoped like the other CRM reports: superadmin sees all,
an agency admin sees their own roster.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from config import get_settings, get_engine
from utils.auth import require_permission, apply_scope, AuthContext
from utils.permissions import Permission, ScopeLevel
from services import attendanceService

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter(prefix="/crm/attendance", tags=["CRM - Attendance"])

IST = timezone(timedelta(hours=5, minutes=30))


async def _scope_owner(ctx: AuthContext):
    """None for superadmin/global (all agents); the agency roster for an agency admin;
    otherwise the caller's own id. Mirrors leads._report_scope_owner."""
    if ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value:
        return None
    if ctx.scope_level == ScopeLevel.AGENCY.value:
        return (await apply_scope({}, ctx))["telecaller_id"]
    return ctx.user_id


@router.get("")
async def attendance_month(
    month: Optional[str] = Query(None, description="YYYY-MM (IST); defaults to the current month"),
    ctx: AuthContext = Depends(require_permission(Permission.REPORTS_READ)),
):
    """Monthly attendance matrix: per telecaller, each day's worked hours + Present flag
    (>= 7h span), plus a month summary (days present, total hours)."""
    if month:
        try:
            year, mon = (int(p) for p in month.split("-"))
            datetime(year, mon, 1)  # validate
        except (ValueError, TypeError):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="month must be YYYY-MM")
    else:
        now_ist = datetime.now(IST)
        year, mon = now_ist.year, now_ist.month

    return await attendanceService.month_overview(
        engine, year=year, month=mon, scope_owner_id=await _scope_owner(ctx))
