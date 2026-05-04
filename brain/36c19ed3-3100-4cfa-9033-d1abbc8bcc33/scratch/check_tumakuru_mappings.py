import asyncio
import sys
from pathlib import Path

# Add app and SharedBackend paths
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "SharedBackend" / "src"))

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
        print(f"District: {m.district}, Taluk: {m.taluk}, Outlet: {outlet.outlet_name if outlet else 'Unknown'}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(check_mapping())
