import asyncio
import httpx
from typing import Any
from bg_tasks import spawn


class LeadSquaredClient:
    """Low-level HTTP client for the LeadSquared API.

    Handles authentication, retry-with-backoff, and the
    fire-and-forget vs. blocking request distinction.
    All business logic stays in CRMService.
    """

    _RETRIABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
    _BACKOFF_SCHEDULE = [30, 120, 420, 900]  # seconds between retries

    def __init__(self, base_url: str, access_key: str, secret_key: str, timeout: float = 10.0):
        self._base_url = base_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._timeout = timeout

    def _auth_params(self) -> dict:
        return {"accessKey": self._access_key, "secretKey": self._secret_key}

    async def _execute_with_retry(self, method: str, url: str, params: dict, **kwargs) -> None:
        attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(method, url, params=params, **kwargs)

                if 200 <= response.status_code < 300:
                    print(f"Background Task Success: {url} | Status: {response.status_code}")
                    return

                if response.status_code in self._RETRIABLE_STATUSES and attempt < len(self._BACKOFF_SCHEDULE):
                    await asyncio.sleep(self._BACKOFF_SCHEDULE[attempt])
                    attempt += 1
                    continue

                print(f"Background Task Failed: {response.status_code} | Msg: {response}")
                break

            except httpx.RequestError as e:
                if attempt < len(self._BACKOFF_SCHEDULE):
                    await asyncio.sleep(self._BACKOFF_SCHEDULE[attempt])
                    attempt += 1
                    continue
                print(f"Background Task Request Error: {e}")
                break

    def fire_and_forget(self, method: str, endpoint: str, **kwargs) -> dict:
        """Dispatch a request in the background (non-blocking)."""
        url = f"{self._base_url}{endpoint}"
        params = {**self._auth_params(), **kwargs.pop("params", {})}
        spawn(self._execute_with_retry(method, url, params, **kwargs))
        return {"status": "queued", "message": "Request is being processed in the background."}

    async def request(self, method: str, endpoint: str, **kwargs) -> Any:
        """Make a blocking async request and return the JSON response."""
        url = f"{self._base_url}{endpoint}"
        params = {**self._auth_params(), **kwargs.pop("params", {})}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.request(method, url, params=params, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                print(f"CRM HTTP Error: {e.response.status_code} - {e.response.text}")
                return {"error": True, "details": e.response.json() if e.response.text else "HTTP Error"}
            except Exception as e:
                print(f"CRM Request Error: {e}")
                return {"error": True, "details": str(e)}
