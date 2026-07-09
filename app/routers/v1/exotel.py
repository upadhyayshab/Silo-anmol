"""Exotel telephony routes (Feature 3). Depends only on the `TelephonyProvider`
port — no Exotel imports here; the adapter is injected via `get_telephony_provider`.

The in-browser WebRTC softphone is the ONLY call path — the backend never places
calls (no server-side click-to-call):
  GET  /exotel/softphone-token    — mint the softphone SDK credentials (3.1)
  GET|POST /exotel/inbound        — Programmable-Connect dynamic URL: who to ring (3.2)
  POST /exotel/presence/heartbeat — agent presence for inbound routing (3.3)
  GET|POST /exotel/call           — StatusCallback/Passthru webhook -> timeline (3.4/3.5)

Mounted under /api/v1, so the webhook URL to configure in Exotel is:
    https://<host>/api/v1/exotel/call
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from config import get_settings, get_engine
from core.telephony import TelephonyProvider
from dependencies.telephony_dep import get_telephony_provider
from managers import UserManager
from services import telephonyService, presenceService, exophoneService
from utils.auth import get_current_user_id, require_permission, AuthContext
from utils.constants import TELECALLER_ROLES
from utils.permissions import Permission

logger = logging.getLogger(__name__)

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter(prefix="/exotel", tags=["CRM - Exotel Telephony"])


class HeartbeatRequest(BaseModel):
    status: str = "available"          # available | on_call | away


@router.get("/agent-modes")
async def agent_modes(
    _: AuthContext = Depends(require_permission(Permission.LEADS_MANAGE)),
    provider: TelephonyProvider = Depends(get_telephony_provider),
):
    """Each active telecaller's softphone health (admin view). The WebRTC softphone
    is the only call path, so `sip: None` => the agent cannot place or receive calls.
      - `sip`      their Exotel SIP contact uri (None = not provisioned).
      - `verified` outbound health: False = an unverified device is stuck active
                   outbound (breaks calls, 10725); True = healthy; None when no SIP.
    Display-only. One bulk CCM `/users?fields=devices` read backs the whole list."""
    res = await UserManager(engine).fetch_all(filters={"role": TELECALLER_ROLES, "is_active": True})
    agents = list(res.items)
    status = await provider.agent_device_status([getattr(a, "email", "") or "" for a in agents])
    out = []
    for a in agents:
        email = getattr(a, "email", "") or ""
        st = status.get(email.strip().lower()) or {}
        out.append({
            "uid": a.uid,
            "name": getattr(a, "full_name", "") or email or a.uid,
            "email": email,
            "sip": st.get("sip"),
            "verified": st.get("verified"),
        })
    # Broken first (no SIP, then unverified), then healthy — it's an action list.
    out.sort(key=lambda r: (0 if not r["sip"] else (1 if r["verified"] is False else 2),
                            (r["name"] or "").lower()))
    return out


@router.get("/softphone-token")
async def softphone_token(
    user_id: str = Depends(get_current_user_id),
    provider: TelephonyProvider = Depends(get_telephony_provider),
):
    """Mint the in-browser WebRTC softphone SDK credentials for the logged-in agent.
    Returns {accessToken, userId}; 503 until the Exotel WebRTC onboarding is done."""
    agent = await UserManager(engine).fetch(user_id)
    # Outbound caller-ID = the ExoPhone mapped to this agent's state (default otherwise).
    virtual_number = await exophoneService.desired_vn_for(engine, getattr(agent, "state", None))
    auth = await provider.softphone_auth(
        getattr(agent, "email", "") or "",
        name=getattr(agent, "full_name", "") or "",
        agent_number=getattr(agent, "phone", "") or "",
        virtual_number=virtual_number,
    )
    if not auth:
        raise HTTPException(status_code=503,
                            detail="Softphone not available (Exotel WebRTC SDK not configured)")
    return auth


@router.get("/call-outcome")
async def call_outcome(
    call_sid: str,
    _: str = Depends(get_current_user_id),
    provider: TelephonyProvider = Depends(get_telephony_provider),
):
    """CDR-derived Connected / Not-Connected for a just-ended softphone call, so the
    disposition picker can pre-fill its top level. Polls briefly while the CDR settles.
    Returns `{connected: true|false|null}` — null when it couldn't be determined in time
    (the picker then stays blank)."""
    connected = await telephonyService.probe_call_connected(provider, call_sid)
    return {"connected": connected}


@router.post("/presence/heartbeat")
async def presence_heartbeat(body: HeartbeatRequest,
                             user_id: str = Depends(get_current_user_id)):
    """Telecaller presence ping (Feature 3.3). The frontend calls this every ~20s
    while the agent is on shift; inbound routing rings only agents with a fresh ping."""
    await presenceService.heartbeat(engine, user_id, status=body.status)
    return {"status": "ok"}


@router.api_route("/inbound", methods=["GET", "POST"])
async def inbound_route(request: Request,
                        provider: TelephonyProvider = Depends(get_telephony_provider)):
    """Exotel hits this when an inbound call reaches an ExoPhone, to fetch who to ring
    (Feature 3.2). We answer with the lead's owner if available, else the least-loaded
    available telecaller, else no agent (Exotel's Queue applet then takes the call).

    Wire this as the **CCM Programmable Connect** applet's dynamic URL (Primary URL).
    We answer with JSON (HTTP 200, <5s) dialing the chosen agent as a USER, so the
    value can be their **SIP id** (rings the in-browser softphone) or PSTN phone:
        {"fetch_after_attempt": false, "destination_type": "user",
         "destination": [{"contact_uri": "sip:naveenh37746fa6",
                          "device_contact_uri": "sip:naveenh37746fa6"}]}
    An empty `destination` -> Exotel's "we didn't dial anyone" branch (native
    queue/voicemail). NB the old PSTN-only `{"destination":{"numbers":[...]}}` shape
    silently DROPPED `sip:` values (Exotel dialed nobody — DialWhomNumber empty); this
    is the correct Programmable-Connect schema. Exotel's beta docs disagree on field
    names, so we send both `contact_uri` + `device_contact_uri` (+ `destination_type`)
    to satisfy either version.

    ponytail: unsigned like the other Exotel webhooks — allow-list Exotel's source IPs
    at the LB before prod.
    """
    if request.method == "GET":
        payload = dict(request.query_params)
    elif "application/json" in request.headers.get("content-type", ""):
        payload = dict(await request.json())
    else:
        payload = dict(await request.form())

    caller = payload.get("CallFrom") or payload.get("From") or payload.get("caller") or ""
    route = await telephonyService.resolve_inbound_agent(engine, caller, provider=provider)
    logger.info(f"[exotel] inbound from {caller!r} -> {route.reason} "
                f"telecaller={route.telecaller_id} dial={route.dial_number} lead={route.lead_id}")
    if not route.dial_number:
        return {"fetch_after_attempt": False, "destination": []}
    # Current Beta schema: no `destination_type` (setting it to "user" makes Exotel
    # key on a user UUID `id` we don't send -> it dials nobody). The sip:/+91 form of
    # `contact_uri` tells Exotel the type. Send both uri field names across beta versions.
    return {
        "fetch_after_attempt": False,
        "destination": [{
            "contact_uri": route.dial_number,
            "device_contact_uri": route.dial_number,
        }],
    }


@router.api_route("/call", methods=["GET", "POST"])
async def call_event(request: Request, background_tasks: BackgroundTasks,
                     provider: TelephonyProvider = Depends(get_telephony_provider)):
    """Exotel hits this on call lifecycle events. Two shapes are accepted:
      - API StatusCallback -> POST (form-encoded by default, or JSON)
      - Passthru applet (contact-centre flow) -> GET with query params
    We log in the background and return 200 fast — Exotel retries on non-2xx.

    ponytail: Exotel webhooks are unsigned. Blast radius is small (only logs an
    activity onto an already-matched lead), so we accept open. Lock down by
    allow-listing Exotel's source IPs at the LB, or put a secret in the
    callback URL and check it here, before going past sandbox.
    """
    if request.method == "GET":
        payload = dict(request.query_params)
    elif "application/json" in request.headers.get("content-type", ""):
        payload = dict(await request.json())
    else:
        payload = dict(await request.form())

    background_tasks.add_task(telephonyService.handle_event, engine, provider, payload)
    print(f"[exotel] webhook payload={dict(payload)}")   # full shape to map CCM vs Passthru fields
    return {"status": "ok"}
