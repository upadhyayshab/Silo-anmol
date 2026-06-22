"""Facebook Lead Ads ingestion (Feature 1 — inbound lead source).

The webhook only tells us a lead happened (a `leadgen_id`); the actual field
data is pulled from the Graph API with a Page access token. From there we map
the form fields onto a `LeadCreateRequest` and hand off to `leadService`, which
already resolves the outlet and round-robin assigns a telecaller.
"""
import hashlib
import hmac
import logging
from typing import Optional, Dict, Any

import httpx

from config import get_settings
from services import leadService

logger = logging.getLogger(__name__)
settings = get_settings()

GRAPH = "https://graph.facebook.com"


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

async def fetch_lead(leadgen_id: str, token: Optional[str] = None) -> Dict[str, Any]:
    """Pull the full lead record for a leadgen_id (uses the page token when given)."""
    from services import facebook_mapping  # lazy: single source of Graph fields
    url = f"{GRAPH}/{settings.fb_graph_version}/{leadgen_id}"
    params = {
        "access_token": token or settings.fb_page_access_token,
        "fields": facebook_mapping.GRAPH_LEAD_FIELDS,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------
# Ingestion (runs in a BackgroundTask so the webhook can 200 immediately)
#
# Field mapping is delegated to facebook_mapping (DB-configurable Default Mapping
# + per-form overrides), replacing the old hardcoded _FIELD_ALIASES.
# --------------------------------------------------------------------------

async def _page_routing_state(engine, page_id: Optional[str]) -> Optional[str]:
    """The state a page's leads route to (page region always wins). None if unset."""
    if not page_id:
        return None
    from managers import FacebookPageManager
    rows = await FacebookPageManager(engine).fetch_all(filters={"page_id": page_id})
    return rows.items[0].routing_state if rows.items else None


async def create_from_lead_json(engine, page_id: Optional[str], lead_json: Dict[str, Any]):
    """Map a Graph lead object -> CRM lead. Shared by live ingest and backfill.

    Field mapping comes from facebook_mapping (default + per-form). A lead from a
    deactivated form is skipped (returns (None, False)); an unknown/not-yet-synced
    form still ingests with the default mapping. Per-page routing: the page's
    configured state overrides the form's. Dedup is handled by create_lead.
    Returns (lead, created) — lead is None when skipped.
    """
    from services import facebook_mapping  # lazy: avoid import cycle
    form_id = lead_json.get("form_id")
    if (await facebook_mapping.form_status(engine, form_id)) == "inactive":
        logger.info(f"[fb] form {form_id} inactive; skipping lead {lead_json.get('id')}")
        return None, False

    payload = await facebook_mapping.build_lead_request(engine, lead_json)
    if page_id:
        payload.campaign_data = {**(payload.campaign_data or {}), "page_id": page_id}
    routing_state = await _page_routing_state(engine, page_id)
    if routing_state:
        payload.state = routing_state
    return await leadService.create_lead(
        engine, payload, by_user_id="system", source_label="FB Lead Ads")


async def ingest_leadgen(engine, page_id: Optional[str], leadgen_id: str) -> None:
    try:
        token = await _page_token(page_id)
        lead_json = await fetch_lead(leadgen_id, token)
    except Exception as e:
        logger.error(f"[fb] failed to fetch leadgen {leadgen_id}: {e}")
        return
    try:
        lead, created = await create_from_lead_json(engine, page_id, lead_json)
    except Exception as e:
        logger.error(f"[fb] failed to ingest leadgen {leadgen_id}: {e}")
        return
    if lead is None:
        return  # skipped (deactivated form)
    logger.info(f"[fb] leadgen {leadgen_id} {'created' if created else 'merged'} -> "
                f"lead {lead.uid}")


async def _page_token(page_id: Optional[str]) -> Optional[str]:
    if not page_id:
        return None
    from services import facebook_service  # lazy: avoid import cycle
    return await facebook_service.get_page_token(page_id)
