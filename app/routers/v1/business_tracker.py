import calendar
from datetime import date, datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from config import get_settings, get_engine
from managers import DailyTrackerInputManager
from models import TrackerInputUpsert
from services.tracker_metrics import INPUT_KEYS, build_grid
from services.tracker_queries import REGIONS, fetch_lead_facts, fetch_order_facts
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission, ScopeLevel
from utils.timeutils import IST

settings = get_settings()
engine = get_engine(settings.name)
input_manager = DailyTrackerInputManager(engine)

router = APIRouter(prefix="/business-tracker", tags=["Business Daily Tracker"])


def _month_bounds(month: str):
    try:
        year, mon = (int(x) for x in month.split("-"))
        start = date(year, mon, 1)
    except (ValueError, AttributeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "month must be YYYY-MM")
    end = date(year, mon, calendar.monthrange(year, mon)[1])
    return start, end


def _enforce_scope(ctx: AuthContext, region: str) -> None:
    """`reports:read` is held by OUTLET_MANAGER, MARKETING_EXECUTIVE, CLUSTER_MANAGER and
    STATE_HEAD. Without this fence any outlet manager could read all-India revenue and ad
    spend. The tracker reports whole regions, so anything narrower than STATE is refused.
    """
    if ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value:
        return
    wanted = REGIONS[region]
    if ctx.scope_level == ScopeLevel.STATE.value and wanted is not None:
        allowed = {s.strip().lower() for s in (ctx.states or [])}
        if set(wanted) <= allowed:
            return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "The Business Daily Tracker reports whole regions. Your account is scoped more "
        "narrowly than the region you asked for.",
    )


@router.get("")
async def get_tracker(
    month: str = Query(..., description="YYYY-MM"),
    region: str = Query("ALL"),
    ctx: AuthContext = Depends(require_permission(Permission.REPORTS_READ)),
):
    if region not in REGIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"region must be one of {sorted(REGIONS)}")
    _enforce_scope(ctx, region)
    start, end = _month_bounds(month)
    states = REGIONS[region]

    try:
        async with engine.connect() as conn:
            base = await fetch_order_facts(conn, start, end, states)
            base.update(await fetch_lead_facts(conn, start, end, states))

        stored = await input_manager.fetch_month(start, end, region)
        n = (end - start).days + 1
        for metric_key in INPUT_KEYS:
            # None, not 0.0: a day nobody has keyed in yet is unknown, not zero.
            # Zero-filling makes CPL = spends/leads = 0/25000 render as a real "Rs 0"
            # -- free leads -- instead of an em dash. region=ALL can never hold inputs
            # (PUT rejects it), so this is permanent for that view.
            series = [None] * n
            for d, v in stored.get(metric_key, {}).items():
                series[(d - start).days] = float(v)
            base[metric_key] = series

        today = datetime.now(IST).date()
        days = [start + timedelta(days=i) for i in range(n)]
        grid = build_grid(days, base, today)
        grid["month"] = month
        grid["region"] = region
        grid["as_of"] = datetime.now(IST).isoformat()
        return grid
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to build tracker: {e}")


@router.put("/input", status_code=status.HTTP_204_NO_CONTENT)
async def put_tracker_input(
    payload: TrackerInputUpsert,
    ctx: AuthContext = Depends(require_permission(Permission.TRACKER_WRITE)),
):
    _enforce_scope(ctx, payload.region if payload.region in REGIONS else "ALL")
    if payload.metric_key not in INPUT_KEYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{payload.metric_key} is computed, not an input. Writable: {sorted(INPUT_KEYS)}")
    if payload.region not in REGIONS or payload.region == "ALL":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "region must be KN, AP_TG or PN")
    await input_manager.upsert(payload.tracker_date, payload.region,
                               payload.metric_key, payload.value, ctx.user_id)
