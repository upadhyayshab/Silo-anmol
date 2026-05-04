"""
scripts/migrate_hassan_warehouse_merge.py

ONE-TIME MIGRATION — Run this ONCE to merge the existing Warehouse inventory
(outlet_id IS NULL rows) into the Hassan Outlet pool.

What it does
------------
For every product that has a Warehouse row (outlet_id = NULL):

  Case A – Hassan row already exists for that product:
      • Hassan.quantity          = Warehouse.quantity
      • Hassan.reserved_quantity = Warehouse.reserved_quantity
      • Warehouse row is updated to match the SAME values (both rows will now
        stay in sync via sync_unified_inventory going forward)

  Case B – Hassan row does NOT exist:
      • A new Hassan row is created copying the Warehouse quantity / reserved.
      • Warehouse row is unchanged.


After the migration, BOTH rows will reflect the full combined quantity.
All future writes go through sync_unified_inventory which keeps them in sync.

Usage
-----
    python scripts/migrate_hassan_warehouse_merge.py

Set the database environment variables (DB_HOST, DB_NAME, etc.) before running.

Run with --dry-run to preview changes without writing to the database:

    python scripts/migrate_hassan_warehouse_merge.py --dry-run
"""

import asyncio
import sys
from pathlib import Path

# Ensure stdout handles UTF-8 for icons/symbols if terminal supports it
# This must be done BEFORE any imports that might print Unicode characters
if sys.platform == "win32":
    try:
        import codecs
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ImportError):
        pass

# ── Setup Path ─────────────────────────────────────────────────────────────
# Add project root and app to sys.path to ensure imports work correctly
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

app_path = project_root / "app"
if str(app_path) not in sys.path:
    sys.path.insert(0, str(app_path))

# Setup SharedBackend path (matching logic in app/path_setup.py)
local_shared_backend = project_root / "SharedBackend" / "src"
if local_shared_backend.exists():
    if str(local_shared_backend) not in sys.path:
        sys.path.insert(0, str(local_shared_backend))

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

# Import project-specific modules (after adding app_path to sys.path)
from config import get_settings, get_engine
from utils.constants import HASSAN_OUTLET_ID
from managers.erpManagers import InventorySchema


async def run_migration(dry_run: bool = False):
    settings = get_settings()
    # We use settings.name as the default schema if applicable
    engine = get_engine(settings.name)

    print("=" * 60)
    print("Hassan <-> Warehouse inventory merge migration")
    print(f"HASSAN_OUTLET_ID : {HASSAN_OUTLET_ID}")
    print(f"Database         : {settings.engine_str}")
    print(f"Dry run          : {dry_run}")
    print("=" * 60)

    async with AsyncSession(engine) as session:
        # 1. Fetch all warehouse rows (outlet_id IS NULL)
        warehouse_stmt = select(InventorySchema).where(InventorySchema.outlet_id.is_(None))
        result = await session.execute(warehouse_stmt)
        warehouse_rows = result.scalars().all()

        if not warehouse_rows:
            print("No warehouse rows found (outlet_id IS NULL). Checking for Hassan-only rows...")
        else:
            print(f"\nFound {len(warehouse_rows)} warehouse product rows to process.\n")

        created  = 0
        updated  = 0

        for wh_row in warehouse_rows:
            # 2. Check if a Hassan row already exists for this product
            hassan_stmt = select(InventorySchema).where(
                and_(
                    InventorySchema.product_id == wh_row.product_id,
                    InventorySchema.outlet_id  == HASSAN_OUTLET_ID,
                )
            )
            hassan_result = await session.execute(hassan_stmt)
            hassan_row = hassan_result.scalar_one_or_none()

            if hassan_row is not None:
                # Case A: Overwrite Hassan quantities with Warehouse quantities
                new_qty      = wh_row.quantity
                new_reserved = wh_row.reserved_quantity

                print(
                    f"[UPDATE] product={wh_row.product_id[:8]}... "
                    f"Hassan qty {hassan_row.quantity} -> {new_qty} (from WH) | "
                    f"reserved {hassan_row.reserved_quantity} -> {new_reserved} (from WH)"
                )

                if not dry_run:
                    hassan_row.quantity          = new_qty
                    hassan_row.reserved_quantity = new_reserved
                    # last_updated is handled by onupdate in schema

                    # Mirror the merged total back onto the warehouse row too
                    wh_row.quantity          = new_qty
                    wh_row.reserved_quantity = new_reserved

                updated += 1

            else:
                # Case B: Create a Hassan row with the warehouse quantities
                print(
                    f"[CREATE] product={wh_row.product_id[:8]}... "
                    f"New Hassan row qty={wh_row.quantity}, reserved={wh_row.reserved_quantity}"
                )

                if not dry_run:
                    new_hassan = InventorySchema(
                        product_id       =wh_row.product_id,
                        outlet_id        =HASSAN_OUTLET_ID,
                        quantity         =wh_row.quantity,
                        reserved_quantity=wh_row.reserved_quantity,
                    )
                    session.add(new_hassan)

                created += 1

        # 3. Also check for products that ONLY exist in Hassan (no warehouse row)
        #    and create the mirror warehouse row for them.
        hassan_only_stmt = select(InventorySchema).where(
            InventorySchema.outlet_id == HASSAN_OUTLET_ID
        )
        hassan_only_result = await session.execute(hassan_only_stmt)
        hassan_only_rows = hassan_only_result.scalars().all()

        for h_row in hassan_only_rows:
            # Check if warehouse row exists
            wh_check_stmt = select(InventorySchema).where(
                and_(
                    InventorySchema.product_id == h_row.product_id,
                    InventorySchema.outlet_id.is_(None),
                )
            )
            wh_check_result = await session.execute(wh_check_stmt)
            if wh_check_result.scalar_one_or_none() is None:
                print(
                    f"[CREATE WH] product={h_row.product_id[:8]}... "
                    f"New Warehouse (NULL) row qty={h_row.quantity}, reserved={h_row.reserved_quantity}"
                )
                if not dry_run:
                    new_wh = InventorySchema(
                        product_id       =h_row.product_id,
                        outlet_id        =None,
                        quantity         =h_row.quantity,
                        reserved_quantity=h_row.reserved_quantity,
                    )
                    session.add(new_wh)
                created += 1

        if not dry_run:
            await session.commit()
            print(f"\n✅ Migration complete.")
        else:
            print(f"\n🔍 Dry run complete — no changes written.")

        print(f"   Rows updated : {updated}")
        print(f"   Rows created : {created}")

    await engine.dispose()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    asyncio.run(run_migration(dry_run=dry_run))
