import asyncio
import sys
from pathlib import Path

# Add app directory to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "app"))

from sqlalchemy import select
from config import get_settings, get_engine
from managers import OutletMappingManager, OutletManager

async def check_mapping():
    settings = get_settings()
    engine = get_engine(settings.name)
    
    mapping_manager = OutletMappingManager(engine)
    outlet_manager = OutletManager(engine)
    
    print("Checking mappings for tumakuru...")
    mappings = await mapping_manager.fetch_all(filters={"district": "tumakuru"})
    
    for m in mappings.items:
        outlet = await outlet_manager.fetch(m.outlet_id)
        print(f"ID: {m.uid}, District: {m.district}, Taluk: {m.taluk}, Outlet: {outlet.outlet_name if outlet else 'Unknown'}, Active: {m.is_active}")

    print("\nChecking if Sira Outlet has any mappings...")
    all_outlets = await outlet_manager.fetch_all()
    sira_outlet = next((o for o in all_outlets.items if "sira" in o.outlet_name.lower()), None)
    
    if sira_outlet:
        print(f"Found Sira Outlet: {sira_outlet.outlet_name} (ID: {sira_outlet.uid})")
        sira_mappings = await mapping_manager.fetch_all(filters={"outlet_id": sira_outlet.uid})
        for m in sira_mappings.items:
            print(f"Mapping: {m.district} -> {m.taluk}")
    else:
        print("Sira Outlet not found!")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(check_mapping())
