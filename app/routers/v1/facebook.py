"""Facebook Lead Ads webhook (Feature 1 — inbound lead ingestion).

Mounted under /api/v1, so the callback URL to register in the Meta App is:
    https://<host>/api/v1/webhooks/facebook

GET  : Meta subscription verification — echoes hub.challenge when the token matches.
POST : leadgen notifications — HMAC-verified, then each lead is fetched + created
       in a background task so we can return 200 fast (Meta retries on slow/non-2xx).
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Request, Response, HTTPException, Query

from config import get_settings, get_engine
from services import facebook_leads

logger = logging.getLogger(__name__)

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter(prefix="/webhooks/facebook", tags=["CRM - Facebook Lead Ads"])


@router.get("")
async def verify(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    """Meta calls this once when you click 'Verify and Save' on the webhook."""
    if mode == "subscribe" and token and token == settings.fb_verify_token:
        return Response(content=challenge or "", media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("")
async def receive(request: Request, background_tasks: BackgroundTasks):
    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not facebook_leads.verify_signature(settings.fb_app_secret, raw, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    if payload.get("object") != "page":
        return {"status": "ignored"}

    queued = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "leadgen":
                continue
            leadgen_id = (change.get("value") or {}).get("leadgen_id")
            if leadgen_id:
                background_tasks.add_task(facebook_leads.ingest_leadgen, engine, leadgen_id)
                queued += 1

    logger.info(f"[fb] webhook accepted, queued {queued} leadgen event(s)")
    return {"status": "ok", "queued": queued}
