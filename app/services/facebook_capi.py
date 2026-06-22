"""Meta Conversions API — report CRM lead-stage events back to the dataset.

CRM-wide: every lead's stage change (and creation) fires an event, replacing the
LeadSquared CAPI producer. Matches by hashed phone/email; FB-sourced leads also
carry the leadgen_id. Event names mirror the live dataset verbatim — including the
legacy misspellings (`Engagged`, `Not reacheable`) — so existing custom
conversions / ad optimization keep working.

ponytail: best-effort fire-and-forget POST, no local outbox table — add one if we
need guaranteed retry/audit (plan §12.5).
"""
import hashlib
import logging
import time
from typing import Optional

import httpx

from config import get_settings
from utils.crm_enums import LeadStage
from utils.dedup_utils import normalize_mobile, normalize_email

logger = logging.getLogger(__name__)
settings = get_settings()

GRAPH = "https://graph.facebook.com"

# ERP stage -> Meta event name. VERBATIM to the live dataset (misspellings kept).
STAGE_EVENT = {
    LeadStage.NEW_LEAD: "New Lead",
    LeadStage.ENGAGED: "Engagged",
    LeadStage.LAPSED: "Lapsed",
    LeadStage.RTU: "RTU",
    LeadStage.FTU: "FTU",
    LeadStage.NOT_QUALIFIED: "Not Qualified",
    LeadStage.NOT_REACHABLE: "Not reacheable",
}


def _sha256(value: Optional[str]) -> Optional[str]:
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def build_event(lead, stage, *, event_time: int) -> Optional[dict]:
    """Build the CAPI event payload, or None if it can't be matched/mapped."""
    stage = stage if isinstance(stage, LeadStage) else LeadStage(stage)
    name = STAGE_EVENT.get(stage)
    if not name:
        return None

    user_data = {}
    phone = normalize_mobile(getattr(lead, "mobile", None))
    if phone:
        user_data["ph"] = _sha256("91" + phone)   # E.164 digits (India), no '+'
    email = normalize_email(getattr(lead, "email", None))
    if email:
        user_data["em"] = _sha256(email)
    leadgen_id = (getattr(lead, "campaign_data", None) or {}).get("leadgen_id")
    if leadgen_id:
        user_data["lead_id"] = leadgen_id
    if not user_data:
        return None   # nothing for Meta to match on

    day = time.strftime("%Y%m%d", time.gmtime(event_time))
    return {
        "event_name": name,
        "event_time": event_time,
        "action_source": "system_generated",
        "event_id": f"{lead.uid}:{name}:{day}",   # dedup (covers backfill + LSQ overlap)
        "user_data": user_data,
    }


async def send_stage_event(lead, stage) -> None:
    """Fire a CAPI event for a lead's stage. Best-effort; never raises."""
    if not settings.fb_capi_dataset_id or not settings.fb_capi_access_token:
        return
    event = build_event(lead, stage, event_time=int(time.time()))
    if not event:
        return
    url = f"{GRAPH}/{settings.fb_graph_version}/{settings.fb_capi_dataset_id}/events"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json={"data": [event]},
                                  params={"access_token": settings.fb_capi_access_token})
            r.raise_for_status()
    except Exception as e:
        logger.error(f"[capi] {event['event_name']} for lead {lead.uid} failed: {e}")


def demo():
    """Self-check: stage->event mapping + payload shape (no network)."""
    class L:
        uid = "lead-1"
        mobile = "+91 98765-43210"
        email = "A@B.com"
        campaign_data = {"leadgen_id": "lg1"}
    ev = build_event(L(), LeadStage.ENGAGED, event_time=0)
    assert ev["event_name"] == "Engagged", ev
    assert ev["user_data"]["lead_id"] == "lg1"
    assert ev["user_data"]["ph"] == _sha256("919876543210")
    assert ev["event_id"] == "lead-1:Engagged:19700101", ev["event_id"]
    # no contact info -> no event
    class Empty:
        uid = "x"; mobile = None; email = None; campaign_data = None
    assert build_event(Empty(), LeadStage.RTU, event_time=0) is None
    print("facebook_capi.demo OK")


if __name__ == "__main__":
    demo()
