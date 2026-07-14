import logging
from datetime import date, timedelta
from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_engine, get_settings
from managers.erpManagers import (
    OutletSchema, ProductSchema, InventorySchema,
    InventoryAuditSchema, InventoryAuditItemSchema
)
from models.erpModels import AuditStatus
from utils.timeutils import ist_today

logger = logging.getLogger(__name__)

# An audit cycle runs Saturday (generated) -> the following Wednesday (closed).
CYCLE_GEN_WEEKDAY = 5      # Mon=0 .. Sat=5
CYCLE_LENGTH_DAYS = 4      # Saturday + 4 = the closing Wednesday


def cycle_start(d: date) -> date:
    """The Saturday on/before `d` — the start of its audit cycle."""
    return d - timedelta(days=(d.weekday() - CYCLE_GEN_WEEKDAY) % 7)


class InventoryAuditService:
    def __init__(self):
        self.engine = get_engine(get_settings().name)

    async def generate_weekly_audits(self):
        """
        Generate weekly inventory audit tasks for all active outlets.
        Runs every Saturday; the cycle's fill window closes the following Wednesday.
        """
        today = ist_today()
        # The Saturday that opens this cycle — the canonical key for "this week's" audit.
        week_start = cycle_start(today)
        logger.info(f"Starting weekly inventory audit generation for cycle starting {week_start}")

        async with AsyncSession(self.engine) as session:
            try:
                # 1. Get all active outlets
                outlets_result = await session.execute(
                    select(OutletSchema).where(OutletSchema.is_active == True)
                )
                active_outlets = outlets_result.scalars().all()
                
                # 2. Get all active products
                products_result = await session.execute(
                    select(ProductSchema).where(ProductSchema.is_active == True)
                )
                active_products = products_result.scalars().all()
                
                audits_created = 0
                for outlet in active_outlets:
                    # Check if an audit already exists for this outlet THIS WEEK.
                    # Keyed on week_start (not audit_date) so a manual "Generate Now"
                    # on any weekday cannot create a duplicate weekly audit.
                    existing_audit_result = await session.execute(
                        select(InventoryAuditSchema)
                        .where(InventoryAuditSchema.outlet_id == outlet.uid)
                        .where(InventoryAuditSchema.week_start == week_start)
                    )
                    if existing_audit_result.scalars().first():
                        logger.info(f"Audit already exists for outlet {outlet.uid} for week {week_start}")
                        continue

                    # Create Audit Schema
                    audit = InventoryAuditSchema(
                        outlet_id=outlet.uid,
                        audit_date=today,
                        week_start=week_start,
                        status=AuditStatus.PENDING
                    )
                    session.add(audit)
                    await session.flush() # To get audit.uid
                    
                    # Fetch current inventory for this outlet
                    inventory_result = await session.execute(
                        select(InventorySchema).where(InventorySchema.outlet_id == outlet.uid)
                    )
                    inventory_map = {inv.product_id: inv.quantity for inv in inventory_result.scalars().all()}
                    
                    # Create Audit Items
                    for product in active_products:
                        # If null or 0, we can use inventory_map.get
                        system_quantity = inventory_map.get(product.uid)
                        if system_quantity is None:
                            system_quantity = 0
                            
                        audit_item = InventoryAuditItemSchema(
                            audit_id=audit.uid,
                            product_id=product.uid,
                            system_quantity=system_quantity,
                            physical_quantity=None
                        )
                        session.add(audit_item)
                        
                    audits_created += 1
                        
                await session.commit()
                logger.info(f"Successfully generated {audits_created} audits for {today}")
                return {"status": "success", "audits_created": audits_created}
                
            except Exception as e:
                await session.rollback()
                logger.error(f"Error generating weekly audits: {e}")
                raise e

    async def close_overdue_audits(self):
        """
        Close audits whose Wednesday deadline has passed without submission.
        Runs after the close day; any still-PENDING audit for a cycle that has
        already closed is moved to CLOSED (a "missed" record) so it stops being
        fillable and no longer lingers next to the new cycle's audit.
        """
        today = ist_today()
        # A cycle that opened on `week_start` closes on week_start + CYCLE_LENGTH_DAYS
        # (the Wednesday). It is overdue once today is past that date.
        cutoff = today - timedelta(days=CYCLE_LENGTH_DAYS)
        logger.info(f"Closing PENDING audits with cycle start on/before {cutoff}")

        async with AsyncSession(self.engine) as session:
            try:
                result = await session.execute(
                    update(InventoryAuditSchema)
                    .where(InventoryAuditSchema.status == AuditStatus.PENDING)
                    .where(InventoryAuditSchema.week_start <= cutoff)
                    .values(status=AuditStatus.CLOSED)
                )
                await session.commit()
                closed = result.rowcount or 0
                logger.info(f"Closed {closed} overdue audits")
                return {"status": "success", "audits_closed": closed}
            except Exception as e:
                await session.rollback()
                logger.error(f"Error closing overdue audits: {e}")
                raise e

inventory_audit_service = InventoryAuditService()
