
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from app.managers.erpManagers import CustomerOrderManager, CustomerOrderSchema
from app.config import get_settings
import os

async def test_fetch():
    from app.config import get_engine
    from datetime import date, datetime
    settings = get_settings()
    engine = get_engine(settings.name)
    
    manager = CustomerOrderManager(engine)
    
    # 1. Test :eq with date (coerced by filtering_dependency and converted in orders.py)
    filters_eq = {
        "expected_delivery_date": date(2026, 5, 4)
    }
    
    # 2. Test :gt with dollar sign (supported by updated base.py)
    filters_gt = {
        "expected_delivery_date": {"$gt": date(2026, 5, 1)}
    }
    
    joins = [CustomerOrderSchema.delivery_person, CustomerOrderSchema.items]
    
    try:
        print(f"Engine URL: {settings.engine_str}")
        
        print("Testing EQUALS filter...")
        orders_eq = await manager.fetch_all(filters=filters_eq, joins=joins, limit=5)
        print(f"Success: {len(orders_eq.items)} orders found for EQUALS")
        
        print("Testing GREATER THAN filter...")
        orders_gt = await manager.fetch_all(filters=filters_gt, joins=joins, limit=5)
        print(f"Success: {len(orders_gt.items)} orders found for GREATER THAN")
        
    except Exception as e:
        print(f"Error Type: {type(e)}")
        print(f"Error Message: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_fetch())
