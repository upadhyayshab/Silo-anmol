from typing import Optional
from datetime import datetime
from utils.constants import HASSAN_OUTLET_ID

def is_hassan_or_warehouse(outlet_id: Optional[str]) -> bool:
    """Check if the outlet is the Hassan outlet or Warehouse (unified pool)"""
    return outlet_id is None or outlet_id == HASSAN_OUTLET_ID

async def sync_unified_inventory(
    inventory_manager,
    InventorySchema,
    product_id: str,
    target_outlet_id: Optional[str],
    quantity_delta: int = 0,
    reserved_delta: int = 0,
    session = None
):
    """
    Synchronize inventory between Hassan Outlet and Warehouse (NULL).
    Hassan Outlet is the source of truth.
    """
    if not is_hassan_or_warehouse(target_outlet_id):
        return

    # 1. Update Hassan Outlet record first (Source of Truth)
    hassan_items = await inventory_manager.fetch_all(
        filters={"product_id": product_id, "outlet_id": HASSAN_OUTLET_ID},
        session=session
    )
    
    new_hassan_quantity = 0
    new_hassan_reserved = 0
    
    if hassan_items.items:
        hassan_item = hassan_items.items[0]
        new_hassan_quantity = max(0, hassan_item.quantity + quantity_delta)
        new_hassan_reserved = max(0, hassan_item.reserved_quantity + reserved_delta)
        await inventory_manager.update(
            hassan_item.uid,
            {
                "quantity": new_hassan_quantity,
                "reserved_quantity": new_hassan_reserved,
                "last_updated": datetime.utcnow()
            },
            session=session
        )
    else:
        # Create Hassan record if it doesn't exist (only if we are adding stock or it's a valid delta)
        new_hassan_quantity = max(0, quantity_delta)
        new_hassan_reserved = max(0, reserved_delta)
        new_item = InventorySchema(
            product_id=product_id,
            outlet_id=HASSAN_OUTLET_ID,
            quantity=new_hassan_quantity,
            reserved_quantity=new_hassan_reserved,
            last_updated=datetime.utcnow()
        )
        await inventory_manager.create(new_item, session=session)

    # 2. Mirror exactly to Warehouse record (NULL)
    warehouse_items = await inventory_manager.fetch_all(
        filters={"product_id": product_id, "outlet_id": None},
        session=session
    )
    
    if warehouse_items.items:
        warehouse_item = warehouse_items.items[0]
        await inventory_manager.update(
            warehouse_item.uid,
            {
                "quantity": new_hassan_quantity,
                "reserved_quantity": new_hassan_reserved,
                "last_updated": datetime.utcnow()
            },
            session=session
        )
    else:
        # Create Warehouse record to match Hassan
        new_item = InventorySchema(
            product_id=product_id,
            outlet_id=None,
            quantity=new_hassan_quantity,
            reserved_quantity=new_hassan_reserved,
            last_updated=datetime.utcnow()
        )
        await inventory_manager.create(new_item, session=session)
