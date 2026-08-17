import logging
from typing import Optional
import httpx
from config import get_settings

logger = logging.getLogger("location_service")


class LocationService:
    """
    Location service for reverse-geocoding coordinates to Indian pincodes.
    Kept for future use / alternative offline polygon resolution.
    Currently disabled / commented out to avoid external Google Maps API dependency.
    """
    def __init__(self):
        settings = get_settings()
        self.api_key = settings.google_maps_api_key

    async def get_pincode_from_coordinates(self, lat: float, lon: float) -> Optional[str]:
        if not self.api_key:
            logger.warning("GOOGLE_MAPS_API_KEY is not configured in settings.")
            return None

        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            "latlng": f"{lat},{lon}",
            "key": self.api_key
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params, timeout=5.0)
                if response.status_code != 200:
                    logger.error(f"Google Maps API returned HTTP {response.status_code}: {response.text}")
                    return None

                data = response.json()
                if data.get("status") != "OK":
                    logger.error(f"Google Maps API error response: {data}")
                    return None

                # Search through results for postal_code
                for result in data.get("results", []):
                    for component in result.get("address_components", []):
                        if "postal_code" in component.get("types", []):
                            pincode = component.get("long_name", "").strip()
                            if pincode:
                                logger.info(f"Resolved ({lat}, {lon}) to pincode {pincode}")
                                return pincode

                logger.warning(f"No postal_code component found for coordinates ({lat}, {lon})")
                return None

        except Exception as e:
            logger.error(f"Exception during reverse-geocoding for ({lat}, {lon}): {e}")
            return None


__all__ = ["LocationService"]
