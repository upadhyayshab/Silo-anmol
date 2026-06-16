"""Facebook Lead Ads ingestion (Feature 1 — inbound lead source).

The webhook only tells us a lead happened (a `leadgen_id`); the actual field
data is pulled from the Graph API with a Page access token. From there we map
the form fields onto a `LeadCreateRequest` and hand off to `leadService`, which
already resolves the outlet and round-robin assigns a telecaller.
"""
import hashlib
import hmac
import logging
import re
from typing import Optional, Dict, Any

import httpx

from config import get_settings
from managers import LeadManager
from models import LeadCreateRequest
from utils.crm_constants import LeadSource
from utils.crm_enums import LeadActivityType
from services import leadService

logger = logging.getLogger(__name__)
settings = get_settings()

GRAPH = "https://graph.facebook.com"

# FB form field name -> our LeadCreateRequest field. Anything not listed is still
# preserved verbatim in custom_fields, so no answer is ever lost.
_FIELD_ALIASES = {
    "phone_number": "mobile",
    "phone": "mobile",
    "email": "email",
    "city": "city",
    "state": "state",
    "province": "state",
    "street_address": "address_line",
    "post_code": "pincode",
    "zip_code": "pincode",
    "zip": "pincode",
}


# --------------------------------------------------------------------------
# Webhook signature (X-Hub-Signature-256: sha256=<hmac of raw body>)
# --------------------------------------------------------------------------

def verify_signature(app_secret: str, raw_body: bytes, header: Optional[str]) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    received = header.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


# --------------------------------------------------------------------------
# Graph API
# --------------------------------------------------------------------------

async def fetch_lead(leadgen_id: str) -> Dict[str, Any]:
    """Pull the full lead record for a leadgen_id."""
    url = f"{GRAPH}/{settings.fb_graph_version}/{leadgen_id}"
    params = {
        "access_token": settings.fb_page_access_token,
        "fields": "id,created_time,form_id,ad_id,adset_id,campaign_id,platform,field_data",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------

def _normalize_mobile(value: Optional[str]) -> Optional[str]:
    """Strip spaces/punctuation and the +91 / 91 India country prefix."""
    if not value:
        return value
    digits = re.sub(r"\D", "", value)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits or None


def map_to_lead_request(lead_json: Dict[str, Any]) -> LeadCreateRequest:
    fields: Dict[str, Any] = {}
    for entry in lead_json.get("field_data", []):
        name = entry.get("name")
        values = entry.get("values") or []
        if name:
            fields[name] = values[0] if values else None

    first_name = fields.get("first_name")
    last_name = fields.get("last_name")
    if not first_name and fields.get("full_name"):
        parts = str(fields["full_name"]).strip().split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else None

    mapped: Dict[str, Any] = {}
    for fb_name, our_name in _FIELD_ALIASES.items():
        if fields.get(fb_name) and our_name not in mapped:
            mapped[our_name] = fields[fb_name]

    return LeadCreateRequest(
        first_name=first_name or "Facebook Lead",
        last_name=last_name,
        mobile=_normalize_mobile(mapped.get("mobile")) or "",
        email=mapped.get("email"),
        city=mapped.get("city"),
        state=mapped.get("state"),
        address_line=mapped.get("address_line"),
        pincode=mapped.get("pincode"),
        source=LeadSource.FB_LEAD_ADS,
        custom_fields=fields,
        campaign_data={
            "leadgen_id": lead_json.get("id"),
            "form_id": lead_json.get("form_id"),
            "ad_id": lead_json.get("ad_id"),
            "adset_id": lead_json.get("adset_id"),
            "campaign_id": lead_json.get("campaign_id"),
            "platform": lead_json.get("platform"),
            "created_time": lead_json.get("created_time"),
        },
    )


# --------------------------------------------------------------------------
# Ingestion (runs in a BackgroundTask so the webhook can 200 immediately)
# --------------------------------------------------------------------------

async def ingest_leadgen(engine, leadgen_id: str) -> None:
    try:
        lead_json = await fetch_lead(leadgen_id)
    except Exception as e:
        logger.error(f"[fb] failed to fetch leadgen {leadgen_id}: {e}")
        return

    try:
        payload = map_to_lead_request(lead_json)
    except Exception as e:
        logger.error(f"[fb] failed to map leadgen {leadgen_id}: {e}")
        return

    # Dedup by mobile: if the lead already exists, log a note on it rather than
    # creating a duplicate record (matches the fetch_all(filters=...) pattern).
    if payload.mobile:
        existing = await LeadManager(engine).fetch_all(filters={"mobile": payload.mobile})
        if getattr(existing, "items", None):
            lead = existing.items[0]
            await leadService.record_activity(
                engine, lead.uid, LeadActivityType.NOTE, user_id="system",
                body=f"New Facebook Lead Ads form fill (leadgen {leadgen_id})",
                details=payload.campaign_data,
            )
            logger.info(f"[fb] duplicate mobile {payload.mobile}; noted on lead {lead.uid}")
            return

    lead = await leadService.create_lead(engine, payload, by_user_id="system")
    logger.info(f"[fb] created lead {lead.lead_number} from leadgen {leadgen_id}")
