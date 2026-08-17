"""
Crop-advisory subscription usage metering, per the 3-step flow:
verify (before generating a report, blocks on failure) -> confirm (after a
report is successfully generated, decrements quota) -> rollback (fallback if
verify succeeded but generation then failed).

Endpoints: POST {SUBSCRIPTION_BASE_URL}/api/v1/usage/consume/{verify,confirm,rollback}
Header: x-upid: <upid>
Body: {"action": "crop_advice", "subscription_id": ..., "quantity": ...}

NOTE: the real subscription service's success/failure response shape is not yet
documented -- _call() currently treats any non-2xx response OR a network error
as failure. This may need adjusting once tested against the real dev service
(e.g. to distinguish "quota exceeded" from "service unreachable").
"""
import logging

import httpx

from config import get_settings

logger = logging.getLogger("subscription_service")


class SubscriptionService:
    def __init__(self):
        settings = get_settings()
        self.base_url = settings.subscription_base_url

    async def _call(self, verb: str, upid: str, subscription_id: str, quantity: int = 1) -> tuple[bool, dict]:
        if not self.base_url:
            logger.warning("SUBSCRIPTION_BASE_URL is not configured in settings.")
            return False, {"error": "subscription service not configured"}

        url = f"{self.base_url}/api/v1/usage/consume/{verb}"
        body = {"action": "crop_advice", "subscription_id": subscription_id, "quantity": quantity}

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers={"x-upid": upid}, json=body, timeout=5.0)
                ok = 200 <= response.status_code < 300
                data = response.json() if response.content else {}
                if not ok:
                    logger.warning(f"subscription {verb} failed ({response.status_code}): {data}")
                return ok, data
        except Exception as exc:
            logger.error(f"subscription {verb} error: {exc}")
            return False, {"error": str(exc)}

    async def verify(self, upid: str, subscription_id: str, quantity: int = 1) -> tuple[bool, dict]:
        return await self._call("verify", upid, subscription_id, quantity)

    async def confirm(self, upid: str, subscription_id: str, quantity: int = 1) -> tuple[bool, dict]:
        return await self._call("confirm", upid, subscription_id, quantity)

    async def rollback(self, upid: str, subscription_id: str, quantity: int = 1) -> tuple[bool, dict]:
        return await self._call("rollback", upid, subscription_id, quantity)


__all__ = ["SubscriptionService"]
