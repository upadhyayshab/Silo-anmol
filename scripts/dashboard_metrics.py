"""
Super Admin Dashboard metrics pulled directly from the database.

Metrics computed:
  - Gross Revenue       : SUM(gross_amount)         on delivered orders
  - Total Discounts     : SUM(discount_applied)      on delivered orders
  - Net Revenue         : SUM(total_amount)          on delivered orders
  - Total Commission    : SUM(total_commission)      on delivered orders
  - Prepaid (Online)    : SUM(prepaid_amount)        on delivered orders
  - To be Collected     : Net Revenue - Prepaid
  - Confirmed Coll.     : SUM(amount) from outlet_daily_collections WHERE status=CONFIRMED
  - Delivered Orders    : COUNT(*)                   on delivered orders
  - Active Outlets      : COUNT(*) from outlets WHERE is_active

Period filters (--period):
  all     — no date filter (default)
  today   — order_date = today (IST)
  week    — last 7 days
  month   — current calendar month
  custom  — requires --from-date and --to-date (YYYY-MM-DD)

Usage:
  python scripts/dashboard_metrics.py
  python scripts/dashboard_metrics.py --period today
  python scripts/dashboard_metrics.py --period week
  python scripts/dashboard_metrics.py --period month
  python scripts/dashboard_metrics.py --period custom --from-date 2025-01-01 --to-date 2025-03-31
"""
import asyncio
import argparse
import sys
from pathlib import Path
from datetime import datetime, date, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings, get_engine

IST = timezone(timedelta(hours=5, minutes=30))


def parse_args():
    parser = argparse.ArgumentParser(description="Super Admin Dashboard metrics from DB")
    parser.add_argument(
        "--period",
        choices=["all", "today", "week", "month", "custom"],
        default="all",
        help="Time period for the metrics (default: all)",
    )
    parser.add_argument("--from-date", help="Start date for custom period (YYYY-MM-DD)")
    parser.add_argument("--to-date", help="End date for custom period (YYYY-MM-DD)")
    return parser.parse_args()


def resolve_date_range(period: str, from_date_str: str, to_date_str: str):
    """Returns (start, end) as date objects, or (None, None) for all-time."""
    today = datetime.now(IST).date()

    if period == "all":
        return None, None
    elif period == "today":
        return today, today
    elif period == "week":
        return today - timedelta(days=6), today
    elif period == "month":
        return today.replace(day=1), today
    elif period == "custom":
        if not from_date_str or not to_date_str:
            print("ERROR: --from-date and --to-date are required for custom period.")
            sys.exit(1)
        try:
            start = date.fromisoformat(from_date_str)
            end = date.fromisoformat(to_date_str)
        except ValueError as exc:
            print(f"ERROR: Invalid date format — {exc}")
            sys.exit(1)
        if start > end:
            print("ERROR: --from-date must be <= --to-date.")
            sys.exit(1)
        return start, end


async def fetch_metrics(engine, start: date | None, end: date | None) -> dict:
    date_filter = ""
    params: dict = {}

    if start and end:
        # order_date is stored with timezone; cast to IST date for comparison
        date_filter = (
            "AND (order_date AT TIME ZONE 'Asia/Kolkata')::date "
            "BETWEEN :start AND :end"
        )
        params["start"] = start
        params["end"] = end

    # SQLAlchemy stores the enum *name* in PostgreSQL (e.g. 'DELIVERED', not 'delivered').
    #
    # Pricing breakdown stored per order (see orders.py):
    #   gross_amount    = SUM(qty * cost_price)          — before any discount
    #   discount_applied= SUM(product_manual_discount)   — item-level discounts
    #   prepaid_amount  = amount paid upfront
    #   total_amount    = gross - discount - prepaid      — what is STILL OWED at delivery
    #
    # Dashboard derivations:
    #   Net Revenue          = gross - discounts  = SUM(gross_amount - discount_applied)
    #   Prepaid (Online)     = SUM(prepaid_amount)
    #   To be Collected      = SUM(total_amount)  (already = net - prepaid)
    order_sql = text(f"""
        SELECT
            COUNT(*)                                          AS delivered_orders,
            COALESCE(SUM(gross_amount), 0)                   AS gross_revenue,
            COALESCE(SUM(discount_applied), 0)               AS total_discounts,
            COALESCE(SUM(gross_amount - discount_applied), 0) AS net_revenue,
            COALESCE(SUM(total_commission), 0)               AS total_commission,
            COALESCE(SUM(prepaid_amount), 0)                 AS prepaid_online,
            COALESCE(SUM(total_amount), 0)                   AS to_be_collected
        FROM customer_orders
        WHERE order_status::text = 'DELIVERED'
        {date_filter}
    """)

    # Confirmed collections — filtered by confirmation date if period is set
    collection_date_filter = ""
    collection_params: dict = {}
    if start and end:
        collection_date_filter = "AND date BETWEEN :start AND :end"
        collection_params["start"] = start
        collection_params["end"] = end

    collection_sql = text(f"""
        SELECT COALESCE(SUM(amount), 0) AS confirmed_collections
        FROM outlet_daily_collections
        WHERE confirmation_status = 'CONFIRMED'
        {collection_date_filter}
    """)

    outlet_sql = text("""
        SELECT COUNT(*) AS active_outlets
        FROM outlets
        WHERE is_active = true
    """)

    async with AsyncSession(engine) as session:
        order_row = (await session.execute(order_sql, params)).mappings().one()
        collection_row = (await session.execute(collection_sql, collection_params)).mappings().one()
        outlet_row = (await session.execute(outlet_sql)).mappings().one()

    return {
        "gross_revenue": float(order_row["gross_revenue"]),
        "total_discounts": float(order_row["total_discounts"]),
        "net_revenue": float(order_row["net_revenue"]),
        "total_commission": float(order_row["total_commission"]),
        "prepaid_online": float(order_row["prepaid_online"]),
        "to_be_collected_at_outlet": float(order_row["to_be_collected"]),
        "confirmed_collections": float(collection_row["confirmed_collections"]),
        "delivered_orders": int(order_row["delivered_orders"]),
        "active_outlets": int(outlet_row["active_outlets"]),
    }


def fmt(amount: float) -> str:
    return f"₹{amount:,.2f}"


def pct(part: float, total: float) -> str:
    if total == 0:
        return "0.0%"
    return f"{part / total * 100:.1f}%"


def print_dashboard(metrics: dict, period_label: str):
    net = metrics["net_revenue"]
    print()
    print("=" * 55)
    print("  Super Admin Dashboard")
    print(f"  Period : {period_label}")
    print("=" * 55)
    print(f"  Gross Revenue          {fmt(metrics['gross_revenue']):>18}")
    print(f"    ({metrics['delivered_orders']} delivered orders)")
    print(f"  Total Discounts        {fmt(metrics['total_discounts']):>18}")
    print(f"    (Across delivered orders)")
    print(f"  Net Revenue            {fmt(net):>18}")
    print(f"    (After discounts)")
    print(f"  Total Commission       {fmt(metrics['total_commission']):>18}")
    print(f"    (Outlet commissions)")
    print()
    print(f"  Prepaid (Online)       {fmt(metrics['prepaid_online']):>18}")
    print(f"    ({pct(metrics['prepaid_online'], net)} of net revenue)")
    print(f"  To be Collected        {fmt(metrics['to_be_collected_at_outlet']):>18}")
    print(f"    ({pct(metrics['to_be_collected_at_outlet'], net)} of net revenue)")
    print(f"  Confirmed Collections  {fmt(metrics['confirmed_collections']):>18}")
    print(f"    ({pct(metrics['confirmed_collections'], net)} of net revenue)")
    print()
    print(f"  Delivered Orders       {metrics['delivered_orders']:>18,}")
    print(f"    (Across {metrics['active_outlets']} active outlets)")
    print("=" * 55)
    print()


async def main():
    args = parse_args()
    start, end = resolve_date_range(args.period, args.from_date, args.to_date)

    if args.period == "all":
        period_label = "All Time"
    elif args.period == "today":
        period_label = f"Today ({datetime.now(IST).date()})"
    elif args.period == "week":
        period_label = f"Last 7 days ({start} – {end})"
    elif args.period == "month":
        period_label = f"This Month ({start} – {end})"
    else:
        period_label = f"Custom ({start} – {end})"

    settings = get_settings()
    engine = get_engine(settings.name)

    print(f"Connecting to {settings.db_host or 'local SQLite'}...")
    try:
        metrics = await fetch_metrics(engine, start, end)
    finally:
        await engine.dispose()

    print_dashboard(metrics, period_label)


if __name__ == "__main__":
    asyncio.run(main())
