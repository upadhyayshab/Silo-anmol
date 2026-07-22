"""AiSensy (WhatsApp) Webhook ingestion service.

Processes incoming WhatsApp messages and triggers lead generation
for messages sent by users/contacts.
"""
import logging
from typing import Dict, Any

from models.leadModels import LeadCreateRequest
from utils.crm_constants import LeadSource
from services import leadService

logger = logging.getLogger(__name__)

def _normalize_phone(phone: str) -> str:
    """Normalize phone number to 10 digits."""
    if not phone:
        return ""
    # Strip '+' if present
    clean_phone = phone.lstrip('+')
    # If starts with 91 and length is 12, strip 91
    if len(clean_phone) > 10 and clean_phone.startswith('91'):
        clean_phone = clean_phone[2:]
    # Return last 10 digits as fallback if still longer
    return clean_phone[-10:] if len(clean_phone) >= 10 else clean_phone

async def ingest_webhook(engine, payload: Dict[str, Any]) -> None:
    """Process an AiSensy webhook payload in a background task."""
    try:
        topic = payload.get("topic")
        
        # We handle message creation/reception events
        if topic not in ("message.created", "message.sender.user", "message.received"):
            logger.debug(f"[aisensy] Ignored topic: {topic}")
            return
            
        # Resources can be under "resource" or "notification" (based on stoplight docs)
        resource = payload.get("resource") or payload.get("notification") or {}
        message = resource.get("message") or {}
        contact = resource.get("contact") or {}
        
        if not message or not contact:
            logger.warning("[aisensy] Missing message or contact object in payload")
            return
            
        # Ignore messages sent by agents, system, or assistant to prevent loops
        sender = message.get("sender")
        if sender not in ("user", "contact"):
            logger.debug(f"[aisensy] Ignored outbound message from sender: {sender}")
            return
            
        phone = contact.get("phone") or contact.get("wa_id") or contact.get("mobile")
        if not phone:
            logger.warning("[aisensy] Contact object missing phone number")
            return
            
        normalized_mobile = _normalize_phone(phone)
        
        # Extract name
        name = contact.get("name") or contact.get("first_name") or "WhatsApp Lead"
        
        # Extract message text (can be nested or string depending on message type)
        message_text = ""
        msg_type = message.get("type", "")
        if msg_type == "text":
            text_field = message.get("text")
            if isinstance(text_field, dict):
                message_text = text_field.get("body", "")
            else:
                message_text = str(text_field)
        elif msg_type == "interactive":
            interactive = message.get("interactive", {})
            message_text = interactive.get("button_reply", {}).get("title", "") or interactive.get("list_reply", {}).get("title", "")
        elif msg_type == "button":
            message_text = message.get("button", {}).get("text", "")
        else:
            message_text = f"[{msg_type.upper()} MESSAGE]"
            
        notes = f"WhatsApp Inquiry: {message_text}" if message_text else "Incoming WhatsApp Message"
        
        # Build lead payload
        lead_request = LeadCreateRequest(
            mobile=normalized_mobile,
            first_name=name,
            source=LeadSource.WHATSAPP_INBOUND,
            notes=notes
        )
        
        logger.info(f"[aisensy] Creating lead for mobile {normalized_mobile}")
        
        # Call the CRM create_lead service to handle deduplication and assignment
        lead, created = await leadService.create_lead(
            engine=engine,
            payload=lead_request,
            by_user_id="system",
            source_label="WhatsApp"
        )
        
        status_msg = "Created new lead" if created else "Merged into existing lead"
        logger.info(f"[aisensy] {status_msg}: {lead.uid}")
        
    except Exception as e:
        logger.exception(f"[aisensy] Error ingesting webhook: {e}")
