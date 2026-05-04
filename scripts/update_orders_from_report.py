"""
Update database orders with assigned outlets from the CSV report.
Skips any orders assigned to 'Hassan Office Outlet'.
Also updates district and taluk to the normalized values from the CSV.
"""
import asyncio
import csv
import sys
from pathlib import Path

# Add project directories to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).parent.parent / "SharedBackend" / "src"))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings, get_engine
from managers import CustomerOrderManager, CustomerOrderSchema, OutletManager, OutletSchema

CSV_FILE = Path(__file__).parent / "last_2_days_orders_20260501_160316.csv"
SKIP_OUTLET_NAME = "Hassan Office Outlet"

async def update_orders():
    settings = get_settings()
    engine = get_engine(settings.name)
    
    if not CSV_FILE.exists():
        print(f"Error: CSV file not found at {CSV_FILE}")
        return

    print(f"Reading report from {CSV_FILE}...")
    
    async with AsyncSession(engine) as session:
        # 1. Pre-fetch all active outlets for easy lookup
        outlet_result = await session.execute(select(OutletSchema))
        outlets = outlet_result.scalars().all()
        outlet_map = {o.outlet_name.lower(): o.uid for o in outlets}
        
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            
        print(f"Found {len(rows)} rows in CSV. Processing...")
        
        updated_count = 0
        skipped_count = 0
        error_count = 0
        
        for row in rows:
            order_number = row["order_number"]
            assigned_outlet_name = row["assigned_outlet_name"]
            
            # Skip Hassan fallbacks as requested
            if assigned_outlet_name == SKIP_OUTLET_NAME:
                skipped_count += 1
                continue
                
            # Find outlet ID
            outlet_id = outlet_map.get(assigned_outlet_name.lower())
            if not outlet_id:
                print(f"⚠️  Outlet '{assigned_outlet_name}' not found in DB for order {order_number}. Skipping.")
                error_count += 1
                continue
                
            try:
                # Update the order
                # We update assigned_outlet_id, and also correct the district/taluk 
                # to the normalized ones from the CSV.
                q = (
                    update(CustomerOrderSchema)
                    .where(CustomerOrderSchema.order_number == order_number)
                    .values(
                        assigned_outlet_id=outlet_id,
                        district=row["district"],
                        taluk=row["taluk"]
                    )
                )
                await session.execute(q)
                updated_count += 1
                
                if updated_count % 10 == 0:
                    print(f"Progress: Updated {updated_count} orders...")
                    
            except Exception as e:
                print(f"❌ Error updating order {order_number}: {str(e)}")
                error_count += 1

        await session.commit()
        
    print("\n" + "="*40)
    print("UPDATE SUMMARY")
    print("="*40)
    print(f"Total rows processed : {len(rows)}")
    print(f"Orders updated       : {updated_count}")
    print(f"Orders skipped       : {skipped_count} (Hassan Outlet)")
    print(f"Errors encountered   : {error_count}")
    print("="*40)

if __name__ == "__main__":
    asyncio.run(update_orders())
