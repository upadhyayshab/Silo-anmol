"""
HTTP client for Agromonitoring's internal farm-query endpoint
(GET /api/v6/internal/farms/{farmId}, TRD Section 9.2) — used by the wizard's
farmId-based question suppression. Returns None on any failure (farm not
found, Agro unreachable, timeout) rather than raising: callers degrade to
"no farm context" instead of crashing the wizard request (Review Focus).
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger("agro_internal_service")


class AgroUnavailableError(Exception):
    """Agro could not be reached or answered with a server error, as opposed
    to answering that the farm doesn't exist."""


class AgroInternalService:
    def __init__(self, base_url: str, shared_secret: str, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.shared_secret = shared_secret
        self.timeout = timeout

    async def get_farm(self, farm_id: str, user_id: str, raise_if_unavailable: bool = False) -> Optional[dict]:
        """None when the farm doesn't exist (or isn't this user's). With
        raise_if_unavailable, an unreachable Agro raises AgroUnavailableError
        instead of also returning None, so a caller that can't proceed
        without the farm can say "try again" rather than "farm not found"."""
        if not self.base_url:
            logger.warning("AGRO_INTERNAL_BASE_URL is not configured.")
            if raise_if_unavailable:
                raise AgroUnavailableError("AGRO_INTERNAL_BASE_URL is not configured")
            return None

        url = f"{self.base_url}/api/v6/internal/farms/{farm_id}"
        headers = {
            "X-Internal-Service": "crop-recommendation",
            "X-Internal-Secret": self.shared_secret,
            "X-User-Id": user_id,
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            logger.warning(f"Agro internal farm query failed for {farm_id}: {exc}")
            if raise_if_unavailable:
                raise AgroUnavailableError(str(exc)) from exc
            return None

        if response.status_code >= 500 and raise_if_unavailable:
            raise AgroUnavailableError(f"Agro internal farm query returned {response.status_code}")
        if response.status_code != 200:
            logger.info(f"Agro internal farm query non-200 for {farm_id}: {response.status_code}")
            return None

        return response.json()


__all__ = ["AgroInternalService", "AgroUnavailableError"]
