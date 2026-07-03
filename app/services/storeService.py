import httpx
import asyncio
from typing import Dict, Any, Optional
from config import get_settings
from bg_tasks import spawn


class storeService:
    def __init__(self):
        self.store_url = get_settings().store_url
        self._timeout = 30
    
    async def _await_request(self, method: str, endpoint: str, **kwargs) -> Any:
        """Triggers a request and waits for the JSON response (Blocking async)."""
        url = f"{self.store_url}{endpoint}"
        params = kwargs.pop("params", {}) or {}
        print(f"STORE SERVICE REQUEST: {method} {url} kwargs={kwargs}")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.request(method, url, params=params, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                print(f"STORE HTTP Error: {e.response.status_code} - {e.response.text}")
                return {"error": True, "details": e.response.json() if e.response.text else "HTTP Error"}
            except Exception as e:
                print(f"STORE Request Error: {e}")
                return {"error": True, "details": str(e)}

        
    def _fire_and_forget(self, method: str, endpoint: str, **kwargs):
        """Triggers a request in the background (fire and forget)."""
        try:
            spawn(self._await_request(method, endpoint, **kwargs))
        except Exception as e:
            print(f"Failed to create background task for store service: {e}")

    async def update_store_order(self, order_id, status):
        self._fire_and_forget("POST", "/hooks/erp-order-update", json={"order_id": order_id, "status": status})
        return {"status": "queued"}
    

    async def order_cancelled(self, order_id):
        self._fire_and_forget("POST", "/hooks/erp-order-update", json={"order_id": order_id, "status": "Cancelled"})
        return {"status": "queued"}
        
    async def order_fulfilled(self, order_id):
        self._fire_and_forget("POST", "/hooks/erp-order-update", json={"order_id": order_id, "status": "Fulfilled"})
        return {"status": "queued"}
        
    async def order_delivered(self, order_id):
        self._fire_and_forget("POST", "/hooks/erp-order-update", json={"order_id": order_id, "status": "Delivered"})
        return {"status": "queued"}


    async def order_payment_status(self, order_id, payment_status):
        self._fire_and_forget("POST", "/hooks/erp-order-update", json={"order_id": order_id, "payment_status": payment_status})
        return {"status": "queued"}
    
    async def order_refund_status(self, order_id, refund_status):
        self._fire_and_forget("POST", "/hooks/erp-order-update", json={"order_id": order_id, "refund_status": refund_status})
        return {"status": "queued"}
