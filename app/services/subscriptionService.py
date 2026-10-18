"""
Crop-advisory subscription usage metering. How the subscription service
counts (its routers/v1/usage.py, checked 2026-10-18):
- verify: read-only, "used + 1 <= limit?"; reserves nothing.
- confirm: the only step that counts: used + 1, atomically, never past the limit.
- rollback: used - 1, i.e. undoes a confirm.
So: check_quota (verify) before scoring, confirm after crops are returned,
and nothing on a failed or empty run. rollback is only for undoing a confirm.

Endpoints: POST {SUBSCRIPTION_BASE_URL}/api/v1/subscriptions/active
           POST {SUBSCRIPTION_BASE_URL}/api/v1/usage/consume/{verify,confirm,rollback}
Header: x-upid: <upid>
Body (usage): {"action": "crop_advice", "subscription_id": ..., "quantity": ...}

`check_quota` runs the same checks as Farm Monitoring's report quota
(kissan-agromonitoring routers/v5/reports.check_report_quota): the
subscription must be one of the farmer's own and not cancelled, then a
credit must be available.
"""
import logging
from typing import Optional

import httpx
from fastapi import HTTPException

from config import get_settings

logger = logging.getLogger("subscription_service")

_UNAVAILABLE = "Subscription service is temporarily unavailable. Please try again."


def paywall_if_refused(confirmed: bool, data: dict, upid: str, subscription_id: str) -> None:
    """After confirm, for a result about to be delivered: raise 480 when the
    service refused to count it (e.g. the last credit was used on another
    phone between check_quota and confirm), so no unpaid result goes out.
    An unreachable service is logged, not held against the farmer."""
    if confirmed:
        return
    status = (data or {}).get("status")
    if status is not None and 400 <= status < 500:
        raise HTTPException(status_code=480, detail="limit exceeded, subscribe now")
    logger.error(f"subscription confirm failed for upid={upid} subscription_id={subscription_id}: {data}")


class SubscriptionService:
    def __init__(self):
        settings = get_settings()
        self.base_url = settings.subscription_base_url.rstrip("/")

    async def _post(self, path: str, upid: str, json: Optional[dict] = None) -> tuple[Optional[int], dict]:
        """(status code, body); status is None when the service couldn't be reached."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(f"{self.base_url}{path}", headers={"x-upid": upid}, json=json, timeout=5.0)
        except Exception as exc:
            logger.error(f"subscription {path} error: {exc}")
            return None, {"error": str(exc)}
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {"detail": response.text}
        if not 200 <= response.status_code < 300:
            logger.warning(f"subscription {path} failed ({response.status_code}): {data}")
        return response.status_code, data if isinstance(data, dict) else {"data": data}

    async def _call(self, verb: str, upid: str, subscription_id: str, quantity: int = 1) -> tuple[bool, dict]:
        if not self.base_url:
            logger.warning("SUBSCRIPTION_BASE_URL is not configured in settings.")
            return False, {"error": "subscription service not configured"}

        body = {"action": "crop_advice", "subscription_id": subscription_id, "quantity": quantity}
        status, data = await self._post(f"/api/v1/usage/consume/{verb}", upid, json=body)
        return status is not None and 200 <= status < 300, {"status": status, **data}

    async def check_quota(self, upid: str, subscription_id: str, quantity: int = 1) -> None:
        """Raises unless `subscription_id` is one of the farmer's own active
        subscriptions with a crop-advice credit left:
        404 not theirs / none, 400 cancelled, 480 no credit left (paywall),
        503 when the subscription service can't be reached (a farmer must
        not be shown a paywall because our dependency is down)."""
        if not self.base_url:
            logger.error("SUBSCRIPTION_BASE_URL is not configured in settings.")
            raise HTTPException(status_code=503, detail=_UNAVAILABLE)

        status, data = await self._post("/api/v1/subscriptions/active", upid)
        if status is None or status >= 500:
            raise HTTPException(status_code=503, detail=_UNAVAILABLE)
        if status >= 300:
            raise HTTPException(status_code=status, detail=data.get("detail") or "Subscription check failed")
        subscription = next((s for s in data.get("items") or [] if s.get("uid") == subscription_id), None)
        if subscription is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if subscription.get("subscription_status") == "CANCELLED":
            raise HTTPException(status_code=400, detail="Subscription is cancelled")

        body = {"action": "crop_advice", "subscription_id": subscription_id, "quantity": quantity}
        status, usage = await self._post("/api/v1/usage/consume/verify", upid, json=body)
        if status is None or status >= 500:
            raise HTTPException(status_code=503, detail=_UNAVAILABLE)
        no_credit = usage.get("allowed") is False or ("remaining" in usage and (usage.get("remaining") or 0) <= 0)
        if status >= 300 or no_credit:
            # Same text clients already show on the paywall.
            raise HTTPException(status_code=480, detail="limit exceeded, subscribe now")

    async def verify(self, upid: str, subscription_id: str, quantity: int = 1) -> tuple[bool, dict]:
        return await self._call("verify", upid, subscription_id, quantity)

    async def confirm(self, upid: str, subscription_id: str, quantity: int = 1) -> tuple[bool, dict]:
        return await self._call("confirm", upid, subscription_id, quantity)


    async def rollback(self, upid: str, subscription_id: str, quantity: int = 1) -> tuple[bool, dict]:
        return await self._call("rollback", upid, subscription_id, quantity)


__all__ = ["SubscriptionService", "paywall_if_refused"]
