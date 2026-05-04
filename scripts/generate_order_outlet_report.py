"""
Generate a CSV report of orders from the last 2 days showing the outlet that
would be assigned by the new database-based mapping logic.

Resolution priority per order:
  1. pincode  → pypinindia resolves canonical district + taluk
  2. district + taluk directly from the order row (normalized via alias tables)

Usage:  python scripts/generate_order_outlet_report.py
Output: last_2_days_orders_YYYYMMDD_HHMMSS.csv  (written next to this script)
"""
import asyncio
import csv
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import math
from typing import Optional, Tuple, Any

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).parent.parent / "SharedBackend" / "src"))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings, get_engine
from managers import CustomerOrderManager, CustomerOrderSchema, UserSchema
from utils.outlet_assignment import auto_assign_outlet, normalize_location

# OUTPUT_FILE will be generated with a timestamp inside generate_report


def _sanitize(val: Any) -> Optional[str]:
    """Convert value to string, handling None and NaN."""
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    s = str(val).strip()
    return s if s else None


def _resolve_from_pincode(pincode: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Use pypinindia to get the canonical district and taluk for a pincode.
    Returns (district, taluk) — both may be None on failure.
    """
    try:
        from pypinindia import get_pincode_info
        data = get_pincode_info(pincode)
        if not data:
            return None, None
        row = data[0]
        district = _sanitize(row.get("districtname"))
        taluk = _sanitize(row.get("taluk"))
        return district, taluk
    except Exception:
        return None, None


async def _get_telecaller_name(session: AsyncSession, user_id: Optional[str]) -> str:
    if not user_id:
        return ""
    result = await session.execute(
        select(UserSchema.full_name).where(UserSchema.uid == user_id)
    )
    name = result.scalar_one_or_none()
    return name or user_id


async def generate_report(engine):
    now = datetime.now(timezone.utc)
    start_date = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    print(f"Fetching orders from {start_date:%Y-%m-%d} to {end_date:%Y-%m-%d}")

    async with AsyncSession(engine) as session:
        result = await session.execute(
            select(CustomerOrderSchema).where(
                CustomerOrderSchema.order_date >= start_date,
                CustomerOrderSchema.order_date <= end_date,
            ).order_by(CustomerOrderSchema.order_date.desc())
        )
        orders = result.scalars().all()

        if not orders:
            print("No orders found in the last 2 days.")
            return

        print(f"Found {len(orders)} orders. Resolving outlets...")

        rows = []
        for order in orders:
            pincode = _sanitize(getattr(order, "pincode", None))
            raw_district = _sanitize(getattr(order, "district", None))
            raw_taluk = _sanitize(getattr(order, "taluk", None))

            # Priority 1: resolve district/taluk from pincode via pypinindia
            if pincode:
                resolved_district, resolved_taluk = _resolve_from_pincode(pincode)
                if resolved_district:
                    # Apply alias normalization to canonical pypinindia names as well
                    norm_district, norm_taluk = normalize_location(resolved_district, resolved_taluk)
                    source = f"pincode:{pincode}"
                else:
                    # Pincode lookup failed — fall back to order fields
                    norm_district, norm_taluk = normalize_location(raw_district, raw_taluk)
                    source = "order_fields(pincode_failed)"
            else:
                # Priority 2: use district/taluk from the order row
                norm_district, norm_taluk = normalize_location(raw_district, raw_taluk)
                source = "order_fields"

            assigned_outlet = await auto_assign_outlet(
                engine,
                order_id=order.uid,
                district=norm_district,
                taluk=norm_taluk,
                state=getattr(order, "state", None),
                pincode=pincode or None,
            )

            telecaller_name = await _get_telecaller_name(session, getattr(order, "telecaller_id", None))

            order_date = getattr(order, "order_date", None)
            rows.append({
                "order_number": getattr(order, "order_number", ""),
                "district": norm_district or raw_district,
                "taluk": norm_taluk or raw_taluk,
                "assigned_outlet_name": assigned_outlet.outlet_name if assigned_outlet else "Not Assigned",
                "assignment_type": "Fallback" if getattr(assigned_outlet, "is_fallback", False) else ("Mapped" if assigned_outlet else "N/A"),
                "created_at": order_date.strftime("%Y-%m-%d %H:%M:%S") if order_date else "",
                "created_by": telecaller_name,
                "resolution_source": source,
            })

    headers = [
        "order_number", "district", "taluk",
        "assigned_outlet_name", "assignment_type", "created_at",
        "created_by", "resolution_source",
    ]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = Path(__file__).parent / f"last_2_days_orders_{timestamp}.csv"

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nCSV written to: {output_file}")
    print(f"Total rows: {len(rows)}")

    not_assigned = sum(1 for r in rows if r["assigned_outlet_name"] == "Not Assigned")
    if not_assigned:
        print(f"WARNING: {not_assigned} order(s) could not be assigned an outlet.")


async def main():
    settings = get_settings()
    engine = get_engine(settings.name)
    print("Connecting to database...")
    await generate_report(engine)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
