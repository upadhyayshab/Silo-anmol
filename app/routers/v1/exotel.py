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
import json
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from config import get_settings, get_engine
from core.telephony import TelephonyProvider
from dependencies.telephony_dep import get_telephony_provider
from managers import UserManager, AppSettingManager
from services import telephonyService, presenceService, exophoneService
from utils.auth import get_current_user_id, require_permission, AuthContext
from utils.constants import (
    TELECALLER_ROLES, SETTING_DEFAULT_EXOPHONE, SETTING_IVR_DIGITS,
    SETTING_STATE_EXOPHONES,
)
from utils.permissions import Permission

logger = logging.getLogger(__name__)

settings = get_settings()
engine = get_engine(settings.name)

router = APIRouter(prefix="/exotel", tags=["CRM - Exotel Telephony"])


class HeartbeatRequest(BaseModel):
    status: str = "available"          # available | on_call | away


class ExophoneConfigRequest(BaseModel):
    default_exophone: Optional[str] = None
    state_exophones: Optional[dict] = None
    ivr_digits: Optional[dict] = None


@router.get("/agent-modes")
async def agent_modes(
    _: AuthContext = Depends(require_permission(Permission.LEADS_MANAGE)),
    provider: TelephonyProvider = Depends(get_telephony_provider),
):
    """Each active telecaller's softphone health + outbound caller-ID (admin view). The
    WebRTC softphone is the only call path, so `sip: None` => the agent cannot call.
      - `sip`        their Exotel SIP contact uri (None = not provisioned).
      - `verified`   outbound health: False = an unverified device is stuck active
                     outbound (breaks calls, 10725); True = healthy; None when no SIP.
      - `mapped_vn`  the caller-ID Exotel currently presents (None = not mapped yet).
      - `desired_vn` what this agent's state says it should be.
      - `drift`      the two differ -> POST /exotel/remap re-points them.
    Display-only. Two bulk reads back the whole list (CCM devices + usermappings)."""
    res = await UserManager(engine).fetch_all(filters={"role": TELECALLER_ROLES, "is_active": True})
    agents = list(res.items)
    status = await provider.agent_device_status([getattr(a, "email", "") or "" for a in agents])
    default_vn, overrides = await exophoneService.load(engine)
    mappings = await provider.list_usermappings()      # one bulk call, SipSecret stripped
    plan = {p["email"]: p for p in
            exophoneService.build_drift_plan(agents, mappings, default_vn, overrides)}
    out = []
    for a in agents:
        email = getattr(a, "email", "") or ""
        st = status.get(email.strip().lower()) or {}
        row = plan.get(email.strip().lower()) or {}
        out.append({
            "uid": a.uid,
            "name": getattr(a, "full_name", "") or email or a.uid,
            "email": email,
            "sip": st.get("sip"),
            "verified": st.get("verified"),
            "state": row.get("state"),
            "mapped_vn": row.get("mapped_vn"),
            "desired_vn": row.get("desired_vn"),
            "drift": bool(row.get("drift")),
        })
    # Broken first (no SIP, then unverified), then drifting, then healthy — an action list.
    out.sort(key=lambda r: (0 if not r["sip"] else (1 if r["verified"] is False else (2 if r["drift"] else 3)),
                            (r["name"] or "").lower()))
    return out


@router.get("/exophones")
async def list_exophones(
    _: AuthContext = Depends(require_permission(Permission.LEADS_MANAGE)),
    provider: TelephonyProvider = Depends(get_telephony_provider),
):
    """Every ExoPhone on the account with the call-flow it's bound to, for the
    state->ExoPhone picker. `flow_id: null` = not attached to any inbound flow, so calls
    back to it won't reach the CRM."""
    return await provider.list_caller_ids_all()


@router.get("/exophone-config")
async def get_exophone_config(
    _: AuthContext = Depends(require_permission(Permission.CONFIG_READ)),
):
    """The state->ExoPhone map, the default every unmapped state falls back to, and the
    temporary IVR digit->ExoPhone aliases."""
    default_vn, overrides = await exophoneService.load(engine)
    return {"default_exophone": default_vn, "state_exophones": overrides,
            "ivr_digits": await exophoneService.load_ivr_digits(engine)}


@router.put("/exophone-config")
async def put_exophone_config(
    body: ExophoneConfigRequest,
    _: AuthContext = Depends(require_permission(Permission.CONFIG_WRITE)),
):
    """Save the map. Does NOT re-map anyone — call POST /exotel/remap after previewing."""
    mgr = AppSettingManager(engine)
    if body.default_exophone is not None:
        await mgr.set(SETTING_DEFAULT_EXOPHONE, body.default_exophone)
    if body.state_exophones is not None:
        await mgr.set(SETTING_STATE_EXOPHONES, body.state_exophones)
    if body.ivr_digits is not None:
        await mgr.set(SETTING_IVR_DIGITS, body.ivr_digits)
    default_vn, overrides = await exophoneService.load(engine)
    return {"default_exophone": default_vn, "state_exophones": overrides,
            "ivr_digits": await exophoneService.load_ivr_digits(engine)}


@router.post("/remap")
async def remap_caller_ids(
    dry_run: bool = True,
    _: AuthContext = Depends(require_permission(Permission.CONFIG_WRITE)),
    provider: TelephonyProvider = Depends(get_telephony_provider),
):
    """Re-point every drifting agent's outbound caller-ID at their state's ExoPhone.

    `dry_run=true` (the default) writes nothing and returns the plan, so the UI can show
    "N agents will change X -> Y" before anything happens. Applying issues one PUT per
    drifting agent, which rotates that agent's SipSecret — harmless to the WebRTC
    softphone, which authenticates with accessToken+userId.
    """
    res = await UserManager(engine).fetch_all(filters={"role": TELECALLER_ROLES, "is_active": True})
    default_vn, overrides = await exophoneService.load(engine)
    mappings = await provider.list_usermappings()
    plan = [p for p in exophoneService.build_drift_plan(list(res.items), mappings,
                                                        default_vn, overrides) if p["drift"]]
    if dry_run:
        return {"dry_run": True, "planned": plan, "updated": [], "failed": []}

    updated, failed = [], []
    for p in plan:
        ok = await provider.set_virtual_number(p["email"], p["desired_vn"])
        (updated if ok else failed).append(p["email"])
    logger.info(f"[exotel] remap applied: {len(updated)} updated, {len(failed)} failed")
    return {"dry_run": False, "planned": plan, "updated": updated, "failed": failed}


@router.post("/users/{uid}/provision")
async def provision_agent(
    uid: str,
    _: AuthContext = Depends(require_permission(Permission.CONFIG_WRITE)),
    provider: TelephonyProvider = Depends(get_telephony_provider),
):
    """Create the Exotel user + softphone mapping for one telecaller, so they can call.

    Agency telecallers are never in Exotel's address book, so the lazy auto-map on
    softphone open skips them forever (`sip: null` in /agent-modes). This is the only
    path that creates. Identity comes from the ERP user record — no form, no typos —
    and the caller-ID from their state's ExoPhone. Idempotent: re-provisioning an
    already-mapped agent returns their existing SIP id and writes nothing.
    """
    agent = await UserManager(engine).fetch(uid)
    if not agent:
        raise HTTPException(status_code=404, detail="User not found")
    email = (getattr(agent, "email", "") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Agent has no email — Exotel keys users by email")
    virtual_number = await exophoneService.desired_vn_for(engine, getattr(agent, "state", None))
    sip = await provider.provision_user(
        email,
        name=getattr(agent, "full_name", "") or "",
        agent_number=getattr(agent, "phone", "") or "",
        virtual_number=virtual_number,
    )
    if not sip:
        raise HTTPException(status_code=502, detail="Exotel rejected the provisioning request")
    return {"uid": uid, "email": email, "sip": sip, "mapped_vn": virtual_number}


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
    We answer with JSON (HTTP 200, <5s). Per Exotel's docs, `destination` is an ARRAY of
    {contact_uri} objects ONLY; contact_uri is the agent's **SIP id** (sip:... rings the
    in-browser softphone) or a +91 E.164 PSTN number:
        {"fetch_after_attempt": false,
         "destination": [{"contact_uri": "sip:naveenh37746fa6"}]}
    An empty `destination` -> Exotel's "we didn't dial anyone" branch (native
    queue/voicemail). Field-shape history (all confirmed live): the PSTN-only
    `{"destination":{"numbers":[...]}}` shape silently dropped `sip:` values; adding
    `destination_type:"user"` made Exotel key on a user UUID we don't send; and an extra
    `device_contact_uri` key made Exotel drop the destination (Leg2Status null, no dial).
    So we send exactly `[{"contact_uri": ...}]` and nothing else.

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
    # Which ExoPhone the customer dialed -> which state(s) that number serves.
    dialed = payload.get("CallTo") or payload.get("To") or payload.get("called") or ""
    # IVR bridge: today every call lands on the one published number, so `dialed` carries no
    # region. Each Gather branch instead hits `?digit=N`; N is an alias for that region's
    # ExoPhone, which then feeds the same reverse lookup. The digit therefore WINS over
    # `dialed`. Once the 3 numbers are live, drop the applet: no `digit` -> `dialed` decides,
    # and this line stops doing anything. No code change.
    # `digit` rides in the URL we configured, so read the QUERY STRING too — on a POST it is
    # not in `payload` (which is the body), and the IVR would silently route to nobody.
    digit = payload.get("digit") or request.query_params.get("digit") or ""
    by_digit = await exophoneService.exophone_for_digit_db(engine, digit)
    route = await telephonyService.resolve_inbound_agent(
        engine, caller, provider=provider, dialed_number=(by_digit or dialed))
    logger.info(f"[exotel] inbound from {caller!r} to {(by_digit or dialed)!r}"
                f"{f' (ivr digit {digit})' if by_digit else ''}"
                f" -> {route.reason} "
                f"telecaller={route.telecaller_id} dial={route.dial_number} lead={route.lead_id}")
    if not route.dial_number:
        resp = {"fetch_after_attempt": False, "destination": []}
        logger.info(f"[exotel] inbound RESPONSE -> {json.dumps(resp)}")
        return resp
    # CCM Programmable Connect schema (per Exotel docs): `destination` is an array of
    # {contact_uri} objects ONLY. contact_uri = the SIP id (sip:...) for a WebRTC agent, or
    # +91 E.164 for PSTN. No `destination_type`, and NO `device_contact_uri` — the extra key
    # made Exotel drop the destination entirely (Leg2Status null, agent leg never dialed).
    resp = {
        "fetch_after_attempt": False,
        "destination": [{"contact_uri": route.dial_number}],
    }
    logger.info(f"[exotel] inbound RESPONSE -> {json.dumps(resp)}")
    return resp


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
