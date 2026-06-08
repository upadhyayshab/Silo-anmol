from typing import Any, Dict, Optional

from models import SmartpingEventEnqueueRequest
from .smartping_job_service import smartping_job_service


class SmartpingCampaignConfigService:
    """
    Compatibility facade for campaign-specific app code.
    It now resolves campaign metadata from the DB instead of hardcoding campaign names.
    """

    ABANDONED_CART_EVENT_KEY = "cart_abandonment_nudge_1"

    async def send_abandoned_cart_first_message(
        self,
        destination: str,
        user_name: str,
        first_name: str,
        product_name: str,
        savings_amount: str,
        business_event_ref: str,
        context: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
    ):
        payload_context = {
            "first_name": first_name,
            "product_name": product_name,
            "savings_amount": savings_amount,
        }
        if context:
            payload_context.update(context)

        request = SmartpingEventEnqueueRequest(
            business_event_ref=business_event_ref,
            destination=destination,
            user_name=user_name,
            context=payload_context,
            max_retries=max_retries,
        )
        return await smartping_job_service.enqueue_single_event(
            self.ABANDONED_CART_EVENT_KEY,
            request,
        )


smartping_campaigns = SmartpingCampaignConfigService()
