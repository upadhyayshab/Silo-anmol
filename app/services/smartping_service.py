import httpx
import asyncio
from typing import Dict, Any, Optional, List
from config import get_settings

class SmartpingService:
    def __init__(self):
        self.api_key = get_settings().smartping_api_key
        self.base_url = "https://backend.api-wa.co/campaign/smartping/api/v2"
        self._timeout = 30

    async def send_prebuilt_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send a SmartPing payload that has already been built by the caller.
        The API key is injected here so it does not need to be stored in jobs.
        """
        if not self.api_key:
            print("Smartping API key is not configured.")
            return {"error": True, "details": "Smartping API key is missing"}

        outbound_payload = {"apiKey": self.api_key, **payload}
        log_payload = dict(outbound_payload)
        log_payload["apiKey"] = "***"

        print(f"SMARTPING SERVICE REQUEST: POST {self.base_url} payload={log_payload}")

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(self.base_url, json=outbound_payload)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                print(f"Smartping HTTP Error: {e.response.status_code} - {e.response.text}")
                try:
                    details = e.response.json()
                except Exception:
                    details = e.response.text or "HTTP Error"
                return {
                    "error": True,
                    "status_code": e.response.status_code,
                    "details": details,
                }
            except Exception as e:
                print(f"Smartping Request Error: {e}")
                return {"error": True, "details": str(e)}
        
    async def send_campaign_message(
        self,
        campaign_name: str,
        destination: str,
        user_name: str,
        media: Optional[Dict[str, str]] = None,
        template_params: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        attributes: Optional[Dict[str, str]] = None,
        buttons: Optional[List[Dict[str, Any]]] = None,
        carousel_cards: Optional[List[Dict[str, Any]]] = None,
        location: Optional[Dict[str, Any]] = None,
        params_fallback_value: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Sends an automated WhatsApp message via Smartping's API.
        """
        payload = {
            "campaignName": campaign_name,
            "destination": destination,
            "userName": user_name
        }

        if media is not None:
            payload["media"] = media
        if template_params is not None:
            payload["templateParams"] = template_params
        if tags is not None:
            payload["tags"] = tags
        if attributes is not None:
            payload["attributes"] = attributes
        if buttons is not None:
            payload["buttons"] = buttons
        if carousel_cards is not None:
            payload["carouselCards"] = carousel_cards
        if location is not None:
            payload["location"] = location
        if params_fallback_value is not None:
            payload["paramsFallbackValue"] = params_fallback_value

        return await self.send_prebuilt_payload(payload)

    def send_campaign_message_background(
        self,
        campaign_name: str,
        destination: str,
        user_name: str,
        media: Optional[Dict[str, str]] = None,
        template_params: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        attributes: Optional[Dict[str, str]] = None,
        buttons: Optional[List[Dict[str, Any]]] = None,
        carousel_cards: Optional[List[Dict[str, Any]]] = None,
        location: Optional[Dict[str, Any]] = None,
        params_fallback_value: Optional[Dict[str, str]] = None
    ):
        """Triggers a message send in the background (fire and forget)."""
        try:
            asyncio.create_task(
                self.send_campaign_message(
                    campaign_name=campaign_name,
                    destination=destination,
                    user_name=user_name,
                    media=media,
                    template_params=template_params,
                    tags=tags,
                    attributes=attributes,
                    buttons=buttons,
                    carousel_cards=carousel_cards,
                    location=location,
                    params_fallback_value=params_fallback_value
                )
            )
            return {"status": "queued"}
        except Exception as e:
            print(f"Failed to create background task for Smartping service: {e}")
            return {"error": True, "details": str(e)}
