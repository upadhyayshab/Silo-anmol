"""AiSensy (WhatsApp) Webhook Router.

Mounted under /api/v1, so the callback URL to register in AiSensy is:
    https://<host>/api/v1/webhooks/whatsapp

POST : Receives incoming notifications, validates secret if present,
       and delegates to a background task for processing.
GET  : Optional healthcheck / verification endpoint.
"""
import logging
from fastapi import APIRouter, BackgroundTasks, Request, Response, HTTPException, Query

from config import get_settings, get_engine
from services import aisensy_leads

logger = logging.getLogger(__name__)

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter(prefix="/webhooks/whatsapp", tags=["CRM - AiSensy Webhooks"])


def _verify_secret(request: Request, token: str = None) -> bool:
    """Validate webhook secret from header or query param if configured."""
    secret = settings.aisensy_webhook_secret
    if not secret:
        # If no secret is configured, accept all
        return True
        
    # Check query param
    if token == secret:
        return True
        
    # Check headers (AiSensy might send specific headers, e.g. authorization or custom signature)
    # Adjust header name based on exact AiSensy configuration
    if request is not None:
        auth_header = request.headers.get("Authorization", "")
        if auth_header == f"Bearer {secret}" or auth_header == secret:
            return True
            
        x_token = request.headers.get("X-AiSensy-Token", "")
        if x_token == secret:
            return True
        
    return False


@router.get("")
async def verify(token: str = Query(None)):
    """Healthcheck / verification endpoint for webhook setup."""
    if settings.aisensy_webhook_secret and not _verify_secret(None, token):
        raise HTTPException(status_code=403, detail="Verification failed")
    return Response(content="Verified", media_type="text/plain")


@router.post("")
async def receive(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str = Query(None)
):
    """Receive and process incoming AiSensy webhook payload."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
        
    print(payload)

    if not _verify_secret(request, token):
        raise HTTPException(status_code=403, detail="Invalid or missing webhook secret")
        
    # Dispatch processing to a background task so we return 200 OK immediately
    background_tasks.add_task(aisensy_leads.ingest_webhook, engine, payload)
    
    logger.info("[whatsapp] Webhook payload accepted, queued for processing")
    
    return {"status": "ok", "message": "Queued"}
