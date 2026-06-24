"""
Warehouse/factory outlet utilities.

Replaces the hardcoded HASSAN_OUTLET_ID constant with DB-based lookups
so that adding a second warehouse requires no code changes.
"""
from typing import Optional
import sqlalchemy as db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func

from utils.constants import OutletType

# Legacy sentinel: the original Hassan warehouse UID.
# Used only as a final fallback when the DB has no active warehouse record
# (e.g. during first-run / migrations).  Do NOT reference this in business logic.
_LEGACY_WAREHOUSE_ID = "outlets_1bd14da6-954e-4f7d-bbf9-dafa4a6c3cf2"

# ponytail: AP & Telangana share one warehouse (stored with state "Telangana").
# Collapse AP -> TG so the state-based fallback matches that single warehouse.
# Flip the value if the warehouse is stored as "Andhra Pradesh" instead.
_WAREHOUSE_STATE_ALIASES = {"andhra pradesh": "telangana"}


async def get_default_warehouse_id(engine) -> str:
    """Return the UID of the oldest active warehouse outlet.

    Ordered by created_at so the result is deterministic across multiple
    warehouses (the original Hassan warehouse wins). Only a fallback now —
    transfers store an explicit from_outlet_id; NULL is legacy data only.
    """
    from managers import OutletSchema
    async with AsyncSession(engine) as session:
        result = await session.execute(
            db.select(OutletSchema.uid)
            .where(OutletSchema.outlet_type == OutletType.WAREHOUSE)
            .where(OutletSchema.is_active == True)
            .order_by(OutletSchema.created_at.asc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return row if row else _LEGACY_WAREHOUSE_ID


async def get_fallback_warehouse(engine, state: Optional[str] = None):
    """
    Return an active warehouse outlet for fallback outlet assignment.
    Tries to match the order's state first; falls back to any active warehouse.
    """
    from managers import OutletSchema
    async with AsyncSession(engine) as session:
        if state:
            state = _WAREHOUSE_STATE_ALIASES.get(state.lower(), state.lower())
            result = await session.execute(
                db.select(OutletSchema)
                .where(OutletSchema.outlet_type == OutletType.WAREHOUSE)
                .where(OutletSchema.is_active == True)
                .where(func.lower(OutletSchema.state) == state)
                .limit(1)
            )
            outlet = result.scalar_one_or_none()
            if outlet:
                return outlet

        result = await session.execute(
            db.select(OutletSchema)
            .where(OutletSchema.outlet_type == OutletType.WAREHOUSE)
            .where(OutletSchema.is_active == True)
            .limit(1)
        )
        return result.scalar_one_or_none()


def is_warehouse_outlet(outlet_id: Optional[str], warehouse_id: str) -> bool:
    """Return True if outlet_id represents the warehouse (NULL or matches warehouse_id)."""
    return outlet_id is None or outlet_id == warehouse_id
