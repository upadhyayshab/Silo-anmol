"""
Verify the outlet assignment report against live DB logic.

For every row in the CSV it:
  1. Re-fetches the order from DB (to get the raw pincode / district / taluk).
  2. Re-runs the full auto_assign_outlet logic (pincode priority → district/taluk).
  3. Compares the re-computed outlet with what is recorded in the CSV.
  4. Prints a detailed per-row result and a summary at the end.

Usage:
    python scripts/verify_outlet_report.py
    python scripts/verify_outlet_report.py --csv scripts/last_2_days_orders.csv
"""
import asyncio
import csv
import sys
import argparse
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).parent.parent / "SharedBackend" / "src"))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings, get_engine
from managers import CustomerOrderSchema
from utils.outlet_assignment import auto_assign_outlet

DEFAULT_CSV = Path(__file__).parent / "last_2_days_orders(1).csv"
if not DEFAULT_CSV.exists():
    DEFAULT_CSV = Path(__file__).parent / "last_2_days_orders.csv"


# ── result categories ──────────────────────────────────────────────────────────
OK          = "✅ MATCH"
MISMATCH    = "❌ MISMATCH"
FALLBACK    = "⚠️  FALLBACK"   # assigned to fallback outlet (Hassan / first active)
NOT_FOUND   = "🔍 ORDER_NOT_IN_DB"
NO_OUTLET   = "🚫 NO_OUTLET"


async def verify_row(session: AsyncSession, engine, row: dict) -> dict:
    order_number = row["order_number"]
    csv_outlet   = row["assigned_outlet_name"].strip()

    # ── 1. fetch order from DB ─────────────────────────────────────────────────
    result = await session.execute(
        select(CustomerOrderSchema).where(
            CustomerOrderSchema.order_number == order_number
        )
    )
    order = result.scalar_one_or_none()

    if not order:
        return {
            "order_number": order_number,
            "status": NOT_FOUND,
            "csv_outlet": csv_outlet,
            "live_outlet": "-",
            "note": "Order not found in DB",
        }

    pincode      = (getattr(order, "pincode",   None) or "").strip()
    raw_district = (getattr(order, "district",  None) or "").strip()
    raw_taluk    = (getattr(order, "taluk",     None) or "").strip()
    raw_state    = (getattr(order, "state",     None) or "").strip()

    # ── 2. re-run outlet assignment ────────────────────────────────────────────
    live_outlet = await auto_assign_outlet(
        engine,
        order_id  = order.uid,
        pincode   = pincode   or None,
        district  = raw_district or None,
        taluk     = raw_taluk    or None,
        state     = raw_state    or None,
    )

    live_name = live_outlet.outlet_name if live_outlet else "Not Assigned"

    # ── 3. classify ────────────────────────────────────────────────────────────
    if live_name == "Not Assigned":
        status = NO_OUTLET
    elif live_name != csv_outlet:
        status = MISMATCH
    elif "hassan" in live_name.lower():
        status = FALLBACK          # matched but only because of Hassan fallback
    else:
        status = OK

    return {
        "order_number": order_number,
        "status": status,
        "csv_outlet": csv_outlet,
        "live_outlet": live_name,
        "pincode": pincode,
        "district": raw_district,
        "taluk": raw_taluk,
        "note": "",
    }


async def main(csv_path: Path):
    settings = get_settings()
    engine   = get_engine(settings.name)

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("CSV is empty.")
        return

    print(f"Verifying {len(rows)} rows from {csv_path.name} …\n")

    results = []
    async with AsyncSession(engine) as session:
        for i, row in enumerate(rows, 1):
            res = await verify_row(session, engine, row)
            results.append(res)

            # live progress
            flag = res["status"]
            note = ""
            if res["status"] == MISMATCH:
                note = f"  CSV='{res['csv_outlet']}'  →  LIVE='{res['live_outlet']}'"
            elif res["status"] == FALLBACK:
                note = f"  (no specific mapping — fell back to '{res['live_outlet']}')"
            elif res["status"] == NOT_FOUND:
                note = "  (order missing from DB)"
            elif res["status"] == NO_OUTLET:
                note = "  (could not assign any outlet)"

            print(f"[{i:>3}/{len(rows)}] {flag}  {res['order_number']}{note}")

    # ── summary ────────────────────────────────────────────────────────────────
    counts = {s: 0 for s in [OK, MISMATCH, FALLBACK, NOT_FOUND, NO_OUTLET]}
    for r in results:
        counts[r["status"]] += 1

    print("\n" + "═" * 60)
    print("VERIFICATION SUMMARY")
    print("═" * 60)
    print(f"  Total rows       : {len(results)}")
    print(f"  {OK:<16} : {counts[OK]}")
    print(f"  {FALLBACK:<16} : {counts[FALLBACK]}  ← matched but used Hassan fallback")
    print(f"  {MISMATCH:<16} : {counts[MISMATCH]}  ← outlet differs from CSV")
    print(f"  {NO_OUTLET:<16} : {counts[NO_OUTLET]}")
    print(f"  {NOT_FOUND:<16} : {counts[NOT_FOUND]}")

    # ── detail sections ────────────────────────────────────────────────────────
    mismatches = [r for r in results if r["status"] == MISMATCH]
    if mismatches:
        print("\n── MISMATCHES (action required) ──")
        for r in mismatches:
            print(f"  {r['order_number']}")
            print(f"    CSV   : {r['csv_outlet']}")
            print(f"    LIVE  : {r['live_outlet']}")
            print(f"    Input : pincode={r['pincode']!r}  district={r['district']!r}  taluk={r['taluk']!r}")

    fallbacks = [r for r in results if r["status"] == FALLBACK]
    if fallbacks:
        print("\n── FALLBACKS (no DB mapping found, consider adding) ──")
        for r in fallbacks:
            print(f"  {r['order_number']}  pincode={r['pincode']!r}  district={r['district']!r}  taluk={r['taluk']!r}")

    no_outlet = [r for r in results if r["status"] == NO_OUTLET]
    if no_outlet:
        print("\n── COULD NOT ASSIGN ──")
        for r in no_outlet:
            print(f"  {r['order_number']}  pincode={r['pincode']!r}  district={r['district']!r}  taluk={r['taluk']!r}")

    print()
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help="Path to the report CSV (default: last_2_days_orders.csv next to this script)",
    )
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"CSV not found: {args.csv}")
        sys.exit(1)

    asyncio.run(main(args.csv))
