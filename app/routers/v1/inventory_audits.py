from fastapi import APIRouter, HTTPException, Depends, status
from typing import List, Optional
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
import sqlalchemy as db
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_engine, get_settings
from managers.erpManagers import (
    InventoryAuditSchema, InventoryAuditItemSchema, OutletSchema, ProductSchema,
    InventoryAuditManager, InventoryAuditItemManager,
    InventorySchema, CustomerOrderSchema, OrderItemSchema,
)
from models import ListResponse, StatusResponse
from models.erpModels import (
    WeeklyInventoryAuditResponse, WeeklyInventoryAuditItemResponse,
    WeeklyInventoryAuditSubmitRequest, WeeklyInventoryAuditSummaryResponse,
    WeeklyInventoryAuditItemReportResponse, WeeklyInventoryAuditReportResponse,
    AuditOutletRef, AuditCycleRef, AuditStatus
)
from utils.auth import require_permission, apply_scope, outlet_ids_for_state, AuthContext
from utils.permissions import Permission, ScopeLevel
from utils.constants import OrderStatus
from utils.timeutils import ist_now
from utils.functions import ensure_date
from services.inventory_audit_service import inventory_audit_service

router = APIRouter(prefix="/inventory-audits", tags=["Inventory Audits"])
engine = get_engine(get_settings().name)
audit_manager = InventoryAuditManager(engine)
audit_item_manager = InventoryAuditItemManager(engine)


# An audit cycle runs Saturday (generated) -> the following Wednesday (closed).
# week_start is the Saturday that opens the cycle.
CYCLE_GEN_WEEKDAY = 5      # Mon=0 .. Sat=5
CYCLE_LENGTH_DAYS = 4      # Saturday + 4 = the closing Wednesday


def _cycle_start(d: date) -> date:
    """The Saturday on/before `d` — the start of its audit cycle."""
    return d - timedelta(days=(d.weekday() - CYCLE_GEN_WEEKDAY) % 7)


def _cycle_close(week_start: date) -> date:
    """The Wednesday on which the cycle that opened on `week_start` closes."""
    return week_start + timedelta(days=CYCLE_LENGTH_DAYS)


def _week_fields(week_start: Optional[date], audit_date: Optional[date]):
    """Return (week_start, iso_week, iso_year, week_label) for an audit.

    Falls back to deriving the cycle from audit_date for legacy rows that
    predate the week_start column. The label uses the ISO week of the cycle's
    Saturday so it reads like "Week 23 (2026)".
    """
    ws = week_start
    if ws is None and audit_date is not None:
        ws = _cycle_start(audit_date)
    if ws is None:
        return None, None, None, None
    iso_year, iso_week, _ = ws.isocalendar()
    return ws, iso_week, iso_year, f"Week {iso_week} ({iso_year})"


async def _outlet_name_map(outlet_ids):
    """{uid: outlet_name} for the given outlet ids.

    Loads only the name column instead of the full OutletSchema entity, so audit
    endpoints don't break when other features add columns to `outlets` (e.g. the
    clustering work's `cluster_id`).
    """
    ids = [i for i in set(outlet_ids) if i]
    if not ids:
        return {}
    async with AsyncSession(engine) as session:
        rows = (await session.execute(
            select(OutletSchema.uid, OutletSchema.outlet_name)
            .where(OutletSchema.uid.in_(ids))
        )).all()
    return {uid: name for uid, name in rows}


async def _live_inventory_map(pairs):
    """{(outlet_id, product_id): quantity} for the given pairs, ONE batched query.

    `quantity` on the `inventory` table is the live on-hand figure the app actually
    uses today for outlet stock (InventoryAuditService seeds an audit's
    system_quantity from this same column; routers/v1/inventory.py reads outlet
    stock the same way). `available_quantity` on InventoryResponse is marked
    deprecated and just mirrors `quantity` in the simplified system, so there's no
    separate "available" figure to prefer.

    A pair absent from the result has no inventory row -- caller must default with
    `.get(pair)` (returns None), never fabricate 0 for "unknown".
    """
    outlet_ids = {o for o, _ in pairs if o}
    product_ids = {p for _, p in pairs if p}
    if not outlet_ids or not product_ids:
        return {}
    async with AsyncSession(engine) as session:
        rows = (await session.execute(
            select(InventorySchema.outlet_id, InventorySchema.product_id, InventorySchema.quantity)
            .where(InventorySchema.outlet_id.in_(outlet_ids))
            .where(InventorySchema.product_id.in_(product_ids))
        )).all()
    return {(outlet_id, product_id): quantity for outlet_id, product_id, quantity in rows}


async def _delivered_dates_map(pairs, since_floor):
    """{(outlet_id, product_id): [delivered dates, ascending]} for DELIVERED orders
    carrying that product from that outlet, ONE batched query (order_items JOIN
    customer_orders) -- no per-row query.

    Each row's own `submitted_at` cutoff differs (different audits/weeks), so this
    can't filter to the exact cutoff in SQL for every row in one shot. Instead it
    over-fetches from `since_floor` (the earliest submitted_at across the page) and
    callers re-filter per row in Python against their own submitted_at -- see
    `_count_deliveries_since`. Bounded by the page's outlet/product ids, so this
    stays cheap even though it isn't the single tightest-possible query.
    """
    outlet_ids = {o for o, _ in pairs if o}
    product_ids = {p for _, p in pairs if p}
    if not outlet_ids or not product_ids:
        return {}
    if since_floor is None:
        # No row on this page has a submitted_at yet (all pending) -- every date this
        # query could return would be filtered back out to 0 by _count_deliveries_since
        # anyway (it needs a submitted_at to compare against). Skip fetching every
        # DELIVERED order ever for these outlet/product pairs.
        return {}
    query = (
        select(
            CustomerOrderSchema.assigned_outlet_id,
            OrderItemSchema.product_id,
            CustomerOrderSchema.actual_delivery_date,
        )
        .join(OrderItemSchema, OrderItemSchema.order_id == CustomerOrderSchema.uid)
        .where(CustomerOrderSchema.assigned_outlet_id.in_(outlet_ids))
        .where(OrderItemSchema.product_id.in_(product_ids))
        .where(CustomerOrderSchema.order_status == OrderStatus.DELIVERED)
        .where(CustomerOrderSchema.actual_delivery_date.isnot(None))
    )
    if since_floor is not None:
        query = query.where(CustomerOrderSchema.actual_delivery_date >= since_floor)

    async with AsyncSession(engine) as session:
        rows = (await session.execute(query)).all()

    grouped = {}
    for outlet_id, product_id, delivered_at in rows:
        grouped.setdefault((outlet_id, product_id), []).append(delivered_at)
    for key in grouped:
        grouped[key].sort()
    return grouped


def _count_deliveries_since(dates, submitted_at):
    """How many delivery dates fall strictly after the audit's submitted_at.

    `actual_delivery_date` is only DATE-precision (a plain DATE column in prod
    despite the ORM declaring DateTime -- see timeutils.ist_date), so this compares
    at IST-calendar-day granularity via the codebase's existing `ensure_date`
    helper rather than exact instants. A delivery on the SAME calendar day as the
    submission is excluded (not '>=') -- deliberately conservative, since same-day
    ordering between "count submitted" and "delivered" can't be recovered from a
    date-only column, and treating it as included would risk double-counting stock
    that was already reflected in the count.
    """
    if not dates or submitted_at is None:
        return 0
    submitted_day = ensure_date(submitted_at)
    return sum(1 for d in dates if ensure_date(d) > submitted_day)


@router.post("/admin/generate", response_model=StatusResponse)
async def generate_weekly_audits(
    _: AuthContext = Depends(require_permission(Permission.INVENTORY_ADJUST))
):
    """Manually trigger the generation of weekly audits (Admin only)."""
    try:
        result = await inventory_audit_service.generate_weekly_audits()
        return StatusResponse(status="success", message=f"Generated {result['audits_created']} audits.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("", response_model=ListResponse[WeeklyInventoryAuditResponse])
async def list_audits(
    outlet_id: Optional[str] = None,
    audit_date: Optional[date] = None,
    week_start: Optional[date] = None,
    status: Optional[AuditStatus] = None,
    limit: int = 100,
    offset: int = 0,
    sorts: str = "-created_at",
    ctx: AuthContext = Depends(require_permission(Permission.INVENTORY_READ))
):
    """List audits, filtered by outlet, date or ISO week."""
    try:
        # Row scope: outlet mgr -> own outlet, cluster/state -> their outlets, global -> all.
        # Honor an explicit outlet_id for global callers; apply_scope overrides it for scoped roles.
        db_filters = {"outlet_id": outlet_id} if outlet_id else {}
        db_filters = await apply_scope(db_filters, ctx)

        if audit_date:
            db_filters["audit_date"] = audit_date

        if week_start:
            db_filters["week_start"] = _cycle_start(week_start)

        if status:
            db_filters["status"] = status
            
        sort_list = [s.strip() for s in sorts.split(",") if s.strip()]
        
        records = await audit_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=db_filters or None,
            sorts=sort_list,
        )

        name_map = await _outlet_name_map(a.outlet_id for a in records.items)

        responses = []
        for audit in records.items:
            ws, iso_week, iso_year, week_label = _week_fields(audit.week_start, audit.audit_date)
            responses.append(
                WeeklyInventoryAuditResponse(
                    uid=audit.uid,
                    outlet_id=audit.outlet_id,
                    outlet_name=name_map.get(audit.outlet_id),
                    audit_date=audit.audit_date,
                    week_start=ws,
                    iso_week=iso_week,
                    iso_year=iso_year,
                    week_label=week_label,
                    status=audit.status,
                    match_percentage=audit.match_percentage,
                    submitted_at=audit.submitted_at,
                    submitted_by=audit.submitted_by,
                    items=[]
                )
            )

        return ListResponse(items=responses, count=records.count)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/admin/summary", response_model=WeeklyInventoryAuditSummaryResponse)
async def get_audit_summary(
    week_start: Optional[date] = None,
    state: Optional[str] = None,
    _: AuthContext = Depends(require_permission(Permission.INVENTORY_ADJUST))
):
    """Summarise audit completion for a cycle.

    Defaults to the LATEST cycle that has audits (not today's calendar week) so
    the cards reflect the data actually on screen even if generation is off-cadence
    or audits were back-dated.

    `state` is an optional additional narrowing (full state name, e.g. "Karnataka",
    matched case-insensitively via the outlet's `state` column) — it restricts both
    the denominator and the completed/pending lists to that state's outlets.
    """
    try:
        async with AsyncSession(engine) as session:
            # Which cycle to summarise: the requested one, else the most recent
            # cycle present, else this calendar cycle.
            if week_start:
                target_week = _cycle_start(week_start)
            else:
                target_week = await session.scalar(
                    select(func.max(InventoryAuditSchema.week_start))
                )
            if target_week is None:
                target_week = _cycle_start(date.today())

            # Resolve the state filter to outlet ids once, reused across all three
            # queries below. `state_outlet_ids is None` means "no state filter" —
            # ids or ["__none__"] mirrors the sentinel convention used elsewhere
            # (dashboard.py/orders.py/reports.py) so a state with zero outlets
            # matches nothing instead of falling through to "all outlets".
            state_outlet_ids = await outlet_ids_for_state(state) if state else None

            # Active outlets (id + name) — the denominator.
            outlet_query = (
                select(OutletSchema.uid, OutletSchema.outlet_name)
                .where(OutletSchema.is_active == True)
            )
            if state_outlet_ids is not None:
                outlet_query = outlet_query.where(OutletSchema.uid.in_(state_outlet_ids or ["__none__"]))
            outlet_rows = (await session.execute(outlet_query)).all()

            # Outlets that COMPLETED the target cycle.
            completed_query = (
                select(InventoryAuditSchema.outlet_id, OutletSchema.outlet_name)
                .join(OutletSchema, OutletSchema.uid == InventoryAuditSchema.outlet_id)
                .where(InventoryAuditSchema.week_start == target_week)
                .where(InventoryAuditSchema.status == AuditStatus.COMPLETED)
            )
            if state_outlet_ids is not None:
                completed_query = completed_query.where(
                    InventoryAuditSchema.outlet_id.in_(state_outlet_ids or ["__none__"])
                )
            completed_rows = (await session.execute(completed_query)).all()
            completed_ids = {r[0] for r in completed_rows}

            completed_outlets = [AuditOutletRef(outlet_id=r[0], outlet_name=r[1]) for r in completed_rows]
            pending_outlets = [
                AuditOutletRef(outlet_id=uid, outlet_name=name)
                for uid, name in outlet_rows if uid not in completed_ids
            ]

            avg_match_query = (
                select(func.avg(InventoryAuditSchema.match_percentage))
                .where(InventoryAuditSchema.week_start == target_week)
                .where(InventoryAuditSchema.status == AuditStatus.COMPLETED)
            )
            if state_outlet_ids is not None:
                avg_match_query = avg_match_query.where(
                    InventoryAuditSchema.outlet_id.in_(state_outlet_ids or ["__none__"])
                )
            avg_match = await session.scalar(avg_match_query)

            _, iso_week, iso_year, week_label = _week_fields(target_week, None)

            return WeeklyInventoryAuditSummaryResponse(
                total_active_outlets=len(outlet_rows),
                completed_audits=len(completed_outlets),
                average_match_percentage=Decimal(avg_match) if avg_match is not None else None,
                week_start=target_week,
                week_label=week_label,
                completed_outlets=completed_outlets,
                pending_outlets=pending_outlets,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/admin/cycles", response_model=ListResponse[AuditCycleRef])
async def list_audit_cycles(
    _: AuthContext = Depends(require_permission(Permission.INVENTORY_ADJUST))
):
    """List the audit cycles (weeks) that have data, newest first — drives the
    admin week picker."""
    try:
        async with AsyncSession(engine) as session:
            rows = (await session.execute(
                select(
                    InventoryAuditSchema.week_start,
                    func.count(InventoryAuditSchema.uid)
                )
                .group_by(InventoryAuditSchema.week_start)
                .order_by(InventoryAuditSchema.week_start.desc())
            )).all()

            cycles = []
            for ws, count in rows:
                _, iso_week, iso_year, week_label = _week_fields(ws, None)
                cycles.append(AuditCycleRef(week_start=ws, week_label=week_label, audit_count=count))

            return ListResponse(items=cycles, count=len(cycles))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{audit_id}", response_model=WeeklyInventoryAuditResponse)
async def get_audit_details(
    audit_id: str,
    ctx: AuthContext = Depends(require_permission(Permission.INVENTORY_READ))
):
    """Get details of a specific audit including all items."""
    try:
        # Scope fence: a non-GLOBAL caller (outlet/cluster/state mgr) may only view
        # an audit for an outlet they cover. GLOBAL/microservice -> unrestricted.
        is_scoped = not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value)
        scope_outlets = None
        if is_scoped:
            scope_outlet = (await apply_scope({}, ctx)).get("outlet_id")
            scope_outlets = scope_outlet if isinstance(scope_outlet, list) else [scope_outlet]

        async with AsyncSession(engine) as session:
            audit = await session.get(InventoryAuditSchema, audit_id)
            if not audit:
                raise HTTPException(status_code=404, detail="Audit not found")

            if is_scoped and audit.outlet_id not in scope_outlets:
                raise HTTPException(status_code=403, detail="Not authorized to view this audit")

            items_result = await session.execute(
                select(InventoryAuditItemSchema, ProductSchema.product_name)
                .join(ProductSchema, InventoryAuditItemSchema.product_id == ProductSchema.uid)
                .where(InventoryAuditItemSchema.audit_id == audit_id)
            )
            items_rows = items_result.all()

            # Blind count: never expose the system quantity to a scoped (outlet) counter
            # while the audit is still pending — otherwise they can read it from the
            # API response and type it straight back for a fake 100% match.
            mask_system_qty = (
                is_scoped
                and audit.status != AuditStatus.COMPLETED
            )

            ws, iso_week, iso_year, week_label = _week_fields(audit.week_start, audit.audit_date)
            response = WeeklyInventoryAuditResponse(
                uid=audit.uid,
                outlet_id=audit.outlet_id,
                audit_date=audit.audit_date,
                week_start=ws,
                iso_week=iso_week,
                iso_year=iso_year,
                week_label=week_label,
                status=audit.status,
                match_percentage=audit.match_percentage,
                submitted_at=audit.submitted_at,
                submitted_by=audit.submitted_by,
                items=[
                    WeeklyInventoryAuditItemResponse(
                        uid=item.uid,
                        product_id=item.product_id,
                        product_name=name,
                        system_quantity=None if mask_system_qty else item.system_quantity,
                        physical_quantity=item.physical_quantity
                    )
                    for item, name in items_rows
                ]
            )
            return response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{audit_id}/submit", response_model=WeeklyInventoryAuditResponse)
async def submit_audit(
    audit_id: str,
    payload: WeeklyInventoryAuditSubmitRequest,
    ctx: AuthContext = Depends(require_permission(Permission.INVENTORY_WRITE))
):
    """Submit an audit with physical counts."""
    try:
        # Scope fence: a non-GLOBAL caller (outlet/cluster/state mgr) may only submit
        # an audit for an outlet they cover. GLOBAL/microservice -> unrestricted.
        is_scoped = not (ctx.is_microservice or ctx.scope_level == ScopeLevel.GLOBAL.value)
        scope_outlets = None
        if is_scoped:
            scope_outlet = (await apply_scope({}, ctx)).get("outlet_id")
            scope_outlets = scope_outlet if isinstance(scope_outlet, list) else [scope_outlet]

        async with AsyncSession(engine) as session:
            audit = await session.get(InventoryAuditSchema, audit_id)
            if not audit:
                raise HTTPException(status_code=404, detail="Audit not found")

            if audit.status == AuditStatus.COMPLETED:
                raise HTTPException(status_code=400, detail="Audit is already completed and cannot be modified")

            # An audit is fillable until it is explicitly CLOSED by the Wednesday
            # close job — we don't hard-block on the date, so re-dated/back-dated
            # audits and brief job delays don't lock outlets out unexpectedly.
            if audit.status == AuditStatus.CLOSED:
                raise HTTPException(status_code=400, detail="This audit closed on its deadline and can no longer be submitted")

            if is_scoped and audit.outlet_id not in scope_outlets:
                raise HTTPException(status_code=403, detail="Not authorized to submit this audit")

            # Get existing items
            items_result = await session.execute(
                select(InventoryAuditItemSchema).where(InventoryAuditItemSchema.audit_id == audit_id)
            )
            items = items_result.scalars().all()
            items_map = {item.product_id: item for item in items}

            total_items = len(items)

            # An audit must be submitted in full — a partial payload would mark the
            # audit COMPLETED while leaving items uncounted (and counted as mismatches).
            submitted_ids = {si.product_id for si in payload.items}
            missing = set(items_map.keys()) - submitted_ids
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"All {total_items} items must be counted before submitting "
                           f"({len(missing)} missing)."
                )
            if any(si.physical_quantity < 0 for si in payload.items):
                raise HTTPException(status_code=400, detail="Physical quantity cannot be negative.")

            # Refresh system_quantity to the live Available Qty at submission time
            inventory_result = await session.execute(
                select(InventorySchema)
                .where(InventorySchema.outlet_id == audit.outlet_id)
                .where(InventorySchema.product_id.in_(submitted_ids))
            )
            live_inventory_map = {
                inv.product_id: inv.quantity for inv in inventory_result.scalars().all()
            }

            matched_items = 0
            for submitted_item in payload.items:
                if submitted_item.product_id in items_map:
                    db_item = items_map[submitted_item.product_id]
                    db_item.physical_quantity = submitted_item.physical_quantity

                    # Update system_quantity to live Available Qty at submission time
                    live_qty = live_inventory_map.get(submitted_item.product_id)
                    db_item.system_quantity = 0 if live_qty is None else live_qty

                    if db_item.physical_quantity == db_item.system_quantity:
                        matched_items += 1

            # Update Audit status and match percentage
            audit.status = AuditStatus.COMPLETED
            audit.submitted_at = datetime.now(timezone.utc)
            audit.submitted_by = ctx.user_id
            
            if total_items > 0:
                audit.match_percentage = Decimal(matched_items) / Decimal(total_items) * Decimal(100)
            else:
                audit.match_percentage = Decimal(0)
                
            ws, iso_week, iso_year, week_label = _week_fields(audit.week_start, audit.audit_date)
            response = WeeklyInventoryAuditResponse(
                uid=audit.uid,
                outlet_id=audit.outlet_id,
                audit_date=audit.audit_date,
                week_start=ws,
                iso_week=iso_week,
                iso_year=iso_year,
                week_label=week_label,
                status=audit.status,
                match_percentage=audit.match_percentage,
                submitted_at=audit.submitted_at,
                submitted_by=audit.submitted_by,
                items=[]
            )

            await session.commit()

            return response
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/admin/report", response_model=WeeklyInventoryAuditReportResponse)
async def get_audit_report(
    outlet_id: Optional[str] = None,
    product_id: Optional[str] = None,
    week_start: Optional[date] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    state: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    _: AuthContext = Depends(require_permission(Permission.INVENTORY_ADJUST))
):
    """Get a detailed flat report of audit items (Excel-like view).

    Pass `week_start` to scope to a single cycle (the table then matches the
    summary). `start_date`/`end_date` still work for ad-hoc ranges. `state` is an
    optional additional narrowing (full state name, matched case-insensitively via
    the audit's outlet) — an explicit `outlet_id` is more specific and wins if both
    are supplied.
    """
    try:
        filters = {}
        if outlet_id:
            filters["audit.outlet_id"] = outlet_id
        elif state:
            # Join audit -> outlet via the dotted relationship filter (expanded by
            # ERPGenericManager._filter into {"audit": {"outlet_id": ids}}). Sentinel
            # so a state with zero outlets matches nothing, not everything.
            state_outlet_ids = await outlet_ids_for_state(state)
            filters["audit.outlet_id"] = state_outlet_ids or ["__none__"]
        if product_id:
            filters["product_id"] = product_id
        if week_start:
            filters["audit.week_start"] = _cycle_start(week_start)
        elif start_date or end_date:
            date_filter = {}
            if start_date:
                date_filter["$gte"] = start_date
            if end_date:
                date_filter["$lte"] = end_date
            if date_filter:
                filters["audit.audit_date"] = date_filter

        actual_limit = limit if limit > 0 else 0

        # Load product, audit and submitter via the manager, but NOT the full outlet
        # entity — outlet names are resolved separately so a column added to `outlets`
        # by another feature can't break this report.
        result = await audit_item_manager.fetch_all(
            limit=actual_limit,
            offset=offset,
            filters=filters,
            joins=[
                InventoryAuditItemSchema.product,
                [InventoryAuditItemSchema.audit, InventoryAuditSchema.submitter]
            ]
        )

        name_map = await _outlet_name_map(
            item.audit.outlet_id for item in result.items if item.audit
        )

        # Batched (outlet_id, product_id) lookups for the two new columns -- ONE
        # query each across the whole page, never per-row (see _live_inventory_map /
        # _delivered_dates_map docstrings; precedent: attempt_counts_for/
        # load_events_bulk in services/order_events_service.py).
        pairs = {
            (item.audit.outlet_id, item.product_id)
            for item in result.items if item.audit
        }
        submitted_ats = [item.audit.submitted_at for item in result.items
                         if item.audit and item.audit.submitted_at]
        since_floor = min(submitted_ats) if submitted_ats else None

        live_inventory_map = await _live_inventory_map(pairs)
        delivered_dates_map = await _delivered_dates_map(pairs, since_floor)

        responses = []
        for item in result.items:
            # We must gracefully handle missing relations if any
            audit = item.audit
            product = item.product
            submitter = audit.submitter if audit else None

            ws, iso_week, iso_year, week_label = _week_fields(
                audit.week_start if audit else None,
                audit.audit_date if audit else None,
            )

            pair = (audit.outlet_id, item.product_id) if audit else None
            delivery_count = _count_deliveries_since(
                delivered_dates_map.get(pair, []) if pair else [],
                audit.submitted_at if audit else None,
            )

            responses.append(
                WeeklyInventoryAuditItemReportResponse(
                    uid=item.uid,
                    audit_date=audit.audit_date if audit else None,
                    week_start=ws,
                    iso_week=iso_week,
                    iso_year=iso_year,
                    week_label=week_label,
                    outlet_name=name_map.get(audit.outlet_id) if audit else None,
                    status=audit.status if audit else None,
                    match_percentage=audit.match_percentage if audit else None,
                    submitted_at=audit.submitted_at if audit else None,
                    submitted_by_name=submitter.full_name if submitter else None,
                    product_name=product.product_name if product else None,
                    system_quantity=item.system_quantity,
                    physical_quantity=item.physical_quantity,
                    unit_price=product.unit_price if product else None,
                    delivery_count=delivery_count,
                    live_available_quantity=live_inventory_map.get(pair) if pair else None,
                )
            )

        # Basic sorting in python to emulate order_by since base manager fetch_all doesn't support nested sorts
        responses.sort(key=lambda x: (x.audit_date or date.min, x.outlet_name or ""), reverse=True)

        return WeeklyInventoryAuditReportResponse(items=responses, count=result.count, live_at=ist_now())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
