import logging
from datetime import date
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_engine, get_settings
from managers.erpManagers import (
    OutletSchema, ProductSchema, InventorySchema,
    InventoryAuditSchema, InventoryAuditItemSchema
)
from models.erpModels import AuditStatus

logger = logging.getLogger(__name__)

class InventoryAuditService:
    def __init__(self):
        self.engine = get_engine(get_settings().name)

    async def generate_weekly_audits(self):
        """
        Generate weekly inventory audit tasks for all active outlets.
        This runs every Sunday at 2 AM.
        """
        today = date.today()
        logger.info(f"Starting weekly inventory audit generation for {today}")
        
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
                    # Check if an audit already exists for this date
                    existing_audit_result = await session.execute(
                        select(InventoryAuditSchema)
                        .where(InventoryAuditSchema.outlet_id == outlet.uid)
                        .where(InventoryAuditSchema.audit_date == today)
                    )
                    if existing_audit_result.scalars().first():
                        logger.info(f"Audit already exists for outlet {outlet.uid} on {today}")
                        continue
                        
                    # Create Audit Schema
                    audit = InventoryAuditSchema(
                        outlet_id=outlet.uid,
                        audit_date=today,
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

inventory_audit_service = InventoryAuditService()
