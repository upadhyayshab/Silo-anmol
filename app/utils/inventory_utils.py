from typing import Optional
from utils.warehouse_utils import is_warehouse_outlet


def is_hassan_or_warehouse(outlet_id: Optional[str], warehouse_id: Optional[str] = None) -> bool:
    """Return True if outlet_id is None (legacy NULL rows) or matches the warehouse UID."""
    if warehouse_id is None:
        return outlet_id is None
    return is_warehouse_outlet(outlet_id, warehouse_id)
