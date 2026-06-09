import logging
from typing import Dict, Any

from config import get_engine, get_settings
from managers import CustomerOrderManager
from services.smartping_job_service import smartping_job_service
from models import SmartpingEventEnqueueRequest

logger = logging.getLogger(__name__)

async def trigger_smartping_event_bg(order_id: str, event_key: str):
    """
    Background task to enqueue a SmartPing event based on an order.
    """
    try:
        settings = get_settings()
        engine = get_engine(settings.name)
        order_manager = CustomerOrderManager(engine)
        
        # Fetch the order
        order = await order_manager.fetch(order_id)
        if not order:
            logger.warning(f"SmartPing bg task: Order {order_id} not found.")
            return

        # Automatically serialize ALL order columns into a clean dictionary
        context: Dict[str, Any] = {
            column.name: str(getattr(order, column.name))
            for column in order.__table__.columns
            if getattr(order, column.name) is not None
        }

        # Inject custom calculated fields
        gross_amt = float(getattr(order, 'gross_amount', 0) or 0)
        discount = float(getattr(order, 'discount_applied', 0) or 0)
        
        # This gives you exactly gross minus discount (ignoring prepaid amount)
        context["gross_minus_discount"] = str(gross_amt - discount)
        context["amount_after_discount"] = context["gross_minus_discount"] # Alias
        context["discount"] = discount
        # Keep legacy keys for backward compatibility just in case
        context["name"] = order.customer_name
        context["order_number"] = order.order_number
        context["order_amt"] = str(getattr(order, 'total_amount', 0))
        context["expected_delivery_date"] = str(order.expected_delivery_date) if order.expected_delivery_date else ""
        context["phone"] = order.customer_phone
        context["status"] = getattr(order, 'order_status', '')
        context["address"] = order.address_line

        request = SmartpingEventEnqueueRequest(
            business_event_ref=order_id,
            destination=order.customer_phone,
            user_name=order.customer_name,
            context=context,
        )

        await smartping_job_service.enqueue_single_event(
            event_key=event_key,
            payload=request
        )
        logger.info(f"Successfully enqueued SmartPing event '{event_key}' for order {order_id}")

    except Exception as e:
        # Catch all exceptions to prevent the background task from crashing
        logger.error(f"Error in trigger_smartping_event_bg for order {order_id}, event '{event_key}': {str(e)}")
