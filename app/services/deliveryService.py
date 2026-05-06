from pydantic import BaseModel
import httpx
import asyncio
from typing import Dict, Any, Optional, List
from config import get_settings


class ScheduledOrder(BaseModel):
    order_id: str
    address: str
    pincode: str
    latitude: str
    longitude: str
    priority: int

class ScheduledAssignment(BaseModel):
    driver_uid: str
    orders: List[ScheduledOrder]
    total_distance: float

class ScheduledDeliveryRequest(BaseModel):
    outlet_id: str
    assignments: List[ScheduledAssignment]

class deliveryService:
    def __init__(self):
        self.settings = get_settings()
        self.driver_base_url = self.settings.driver_url
        self.driver_secret_key = self.settings.driver_api_key
        self._timeout = 30.0

    async def _await_request(self, method: str, endpoint: str, **kwargs) -> Any:
        """Triggers a request and waits for the JSON response (Blocking async)."""
        url = f"{self.driver_base_url}{endpoint}"
        
        # Ensure headers include the API key
        headers = kwargs.pop("headers", {}) or {}
        headers["x-api-key"] = self.driver_secret_key
        
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.request(method, url, headers=headers, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                print(f"Driver Error: {e.response.status_code} - {e.response.text}")
                return {"error": True, "details": e.response.json() if e.response.text else "HTTP Error"}
            except Exception as e:
                print(f"Driver Request Error: {e}")
                return {"error": True, "details": str(e)}
    
    async def create_scheduled_delivery(self, payload: ScheduledDeliveryRequest) -> Dict[str, Any]:
        """
        Schedules deliveries in the Driver App system.
        Endpoint: POST /api/v1/scheduling
        """
        print(payload.model_dump())
        return await self._await_request("POST", "/api/v1/scheduling", json=payload.model_dump())