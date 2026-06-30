"""ExotelAdapter — the only place Exotel-specific HTTP/field logic lives.

Implements the `TelephonyProvider` port:
  - `connect_call`     -> CCM Make Call API (POST /v2/accounts/{sid}/calls): rings
                          the agent's softphone by `user_id`, then bridges the lead.
  - `resolve_agent_dial` -> telecaller email -> Exotel `user_id` (via the Users API).
  - `list_caller_ids`  -> ExoPhones scoped to the CRM call flow.
  - `parse_event`      -> normalize the call webhook (Passthru / StatusCallback).

Auth is HTTP Basic (api_key:api_token). The CCM APIs (users, calls) live on the
`ccm-api.*` host; voice/ExoPhone APIs on the `api.*` host. Docs:
  https://developer.exotel.com/api/ccm-make-call-agent-connecting-to-a-number
  https://developer.exotel.com/api/users
  https://support.exotel.com/support/solutions/articles/48283-working-with-passthru-applet

Note: this account's Users API does NOT expose agents' SIP/device ids, so we
identify the agent by `user_id` (which it does expose, keyed by email). That also
means we can't map an inbound webhook's SIP id back to an agent — see
`resolve_agent_email`.
"""
import logging
import re
import time
from typing import Any, List, Mapping, Optional

import httpx

from core.telephony import (
    CallDirection, CallEvent, CallEventKind, CallRequest, CallResponse, CallStatus,
    TelephonyProvider,
)

logger = logging.getLogger(__name__)

# Exotel status strings -> our normalized vocabulary. Keys are lowercased + spaces/
# underscores stripped to a hyphen, so "No Answer" / "no_answer" both hit "no-answer".
_STATUS_MAP = {
    "completed": CallStatus.COMPLETED,
    "answered": CallStatus.COMPLETED,
    "in-progress": CallStatus.IN_PROGRESS,
    "in-call": CallStatus.IN_PROGRESS,
    "active": CallStatus.IN_PROGRESS,      # CCM make-call initial state
    "connected": CallStatus.IN_PROGRESS,
    "queued": CallStatus.RINGING,
    "ringing": CallStatus.RINGING,
    "busy": CallStatus.BUSY,
    "no-answer": CallStatus.NO_ANSWER,
    "incomplete": CallStatus.NO_ANSWER,    # Passthru CallType for an unanswered call
    "customer-unanswered": CallStatus.NO_ANSWER,   # CCM call_status (outbound)
    "agent-unanswered": CallStatus.NO_ANSWER,
    "missed": CallStatus.NO_ANSWER,
    "customer-busy": CallStatus.BUSY,
    "agent-busy": CallStatus.BUSY,
    "failed": CallStatus.FAILED,
    "canceled": CallStatus.CANCELED,
    "cancelled": CallStatus.CANCELED,
}
# Statuses that mean the call is over -> log the outcome.
_TERMINAL = {CallStatus.COMPLETED, CallStatus.BUSY, CallStatus.NO_ANSWER,
             CallStatus.FAILED, CallStatus.CANCELED}


def _norm_status(raw: Optional[str]) -> CallStatus:
    key = (raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    return _STATUS_MAP.get(key, CallStatus.FAILED)


def _e164_in(num: Optional[str]) -> str:
    """Indian number -> E.164 (`+91XXXXXXXXXX`), which the CCM call API requires."""
    digits = re.sub(r"\D", "", str(num or ""))
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    return ("+" + digits) if digits else str(num or "")


def _resp_data(body: Any) -> dict:
    """The single `data` object from Exotel's `{response: [{data: {...}}]}` envelope."""
    if isinstance(body, dict):
        resp = body.get("response")
        if isinstance(resp, list) and resp and isinstance(resp[0], dict):
            return resp[0].get("data") or {}
        if isinstance(resp, dict):
            return resp.get("data") or {}
        if isinstance(body.get("data"), dict):
            return body["data"]
    return {}


def _extract_users(body: Any) -> List[dict]:
    """User objects from the Users API envelope `{response: [{data: {...}}, ...]}`."""
    if not isinstance(body, dict):
        return body if isinstance(body, list) else []
    resp = body.get("response")
    if isinstance(resp, list):
        out = []
        for item in resp:
            if isinstance(item, dict):
                d = item.get("data")
                out.append(d if isinstance(d, dict) else item)
        return [u for u in out if isinstance(u, dict)]
    if isinstance(resp, dict):
        d = resp.get("data")
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
    d = body.get("data")
    return d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])


def _parse_overrides(raw: str) -> dict:
    """`a@x:b@y, c@x:d@y` -> {crm_email: exotel_email} (both lowercased), for the few
    agents whose CRM login email differs from their Exotel user email."""
    out: dict = {}
    for pair in (raw or "").split(","):
        if ":" in pair:
            k, v = pair.split(":", 1)
            k, v = k.strip().lower(), v.strip().lower()
            if k and v:
                out[k] = v
    return out


def _emails_to_userids(users: List[dict]) -> dict:
    """{lowercased email -> Exotel user_id} from a list of Users API user objects."""
    out: dict = {}
    for u in users:
        email = (u.get("email") or "").strip().lower()
        uid = u.get("id")
        if email and uid:
            out[email] = uid
    return out


def _exophones_from_body(body: Any, flow_id: str = "") -> List[dict]:
    """ExoPhones from a v2_beta IncomingPhoneNumbers response, as
    `{number, label}`. If `flow_id` is set, keep only ExoPhones whose `voice_url`
    references that flow — i.e. the ones running the CRM call flow."""
    rows = body
    if isinstance(body, dict):
        rows = body.get("incoming_phone_numbers") or (body.get("response") or {}).get("data") or []
    if isinstance(rows, dict):
        rows = [rows]
    out: List[dict] = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        number = r.get("phone_number") or r.get("PhoneNumber")
        if not number:
            continue
        if flow_id and flow_id not in str(r.get("voice_url") or ""):
            continue
        out.append({"number": number, "label": r.get("friendly_name") or number})
    return out


def _make_call_body(request: CallRequest, default_caller: str, status_callback: str) -> dict:
    """Build the CCM Make Call request body. `agent_number` carries the agent's
    Exotel `user_id` (or a `sip:` contact uri); the API rings that agent's device."""
    agent = request.agent_number or ""
    frm = ({"user_contact_uri": agent} if agent.lower().startswith("sip:")
           else {"user_id": agent})
    body = {
        "from": frm,
        "to": {"customer_contact_uri": _e164_in(request.lead_number)},   # NB: key is customer_contact_uri (Exotel's error mislabels it "to.contact_uri")
        "virtual_number": _e164_in(request.caller_id or default_caller),
    }
    if status_callback:
        body["status_callback"] = [
            {"event": "terminal", "url": status_callback},
            {"event": "answered", "url": status_callback},
        ]
    if request.reference:
        body["custom_field"] = request.reference
    return body


def _first(payload: Mapping[str, Any], *keys: str) -> Optional[str]:
    """First real value among `keys`. Exotel's Passthru sends the literal string
    "null" for empty fields (RecordingUrl, DialCallStatus, ...) — treat that, and
    blanks, as absent. Field names also vary by call flow, hence the fallbacks."""
    for k in keys:
        v = payload.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() != "null":
            return s
    return None


class ExotelAdapter(TelephonyProvider):
    def __init__(self, sid: str, api_key: str, api_token: str,
                 caller_id: str = "", subdomain: str = "api.exotel.com",
                 status_callback: str = "", ccm_subdomain: str = "ccm-api.exotel.com",
                 crm_flow_id: str = "", email_overrides: str = "", sip_map: str = "",
                 app_id: str = "", app_secret: str = "", app_entity: str = "app",
                 integrations_host: str = "integrationscore.mum1.exotel.com"):
        self._sid = sid
        self._auth = (api_key, api_token)
        self._caller_id = caller_id           # ExoPhone / virtual DID shown to the lead
        self._status_callback = status_callback  # public URL Exotel posts call events to
        self._exophones_url = f"https://{subdomain}/v2_beta/Accounts/{sid}/IncomingPhoneNumbers"
        self._calls_base = f"https://{subdomain}/v1/Accounts/{sid}/Calls"  # Voice CDR (call details)
        self._ccm_base = f"https://{ccm_subdomain}/v2/accounts/{sid}"  # Users + Calls (CCM) APIs
        self._crm_flow_id = str(crm_flow_id)  # only list ExoPhones whose flow is the CRM app
        self._email_overrides = _parse_overrides(email_overrides)  # CRM email -> Exotel email exceptions
        self._sip_map = _parse_overrides(sip_map)  # Exotel email -> SIP id, for inbound softphone routing
        # WebRTC softphone SDK auth — App-entity Id/Secret (IP-PSTN-intermix onboarding).
        self._app_id = app_id
        self._app_secret = app_secret
        self._app_entity = app_entity or "app"   # token scope sent to Exotel as Entity
        self._token_url = f"https://{integrations_host}/v2/integrations/token"
        self._usermapping_url = f"https://{integrations_host}/v2/integrations/usermapping"
        self._sdk_token: Optional[str] = None
        self._sdk_token_ts = 0.0
        self._sdk_token_ttl = 30 * 86400   # re-mint monthly; Exotel expires it at 90 days
        self._timeout = 30
        # Exotel email -> SIP id, fetched live from /usermapping and cached (so the static
        # sip_map doesn't have to list every agent). Caches misses too, to avoid refetch storms.
        self._sip_cache: dict = {}   # exotel_email -> (sip_or_None, ts)
        self._sip_cache_ttl = 3600   # 1h; SIP ids are stable once an agent is provisioned
        self._provision_tried: set = set()  # emails auto-map was attempted for (once per process)
        # email -> user_id directory, cached so we don't re-fetch on every call.
        self._dir: Optional[dict] = None
        self._dir_ts = 0.0
        self._dir_ttl = 300   # ponytail: 5-min staleness; a brand-new agent resolves within it
        # ExoPhone list, cached too (read on every lead-page open otherwise).
        self._exo: Optional[List[dict]] = None
        self._exo_ts = 0.0

    async def connect_call(self, request: CallRequest) -> CallResponse:
        """CCM Make Call: ring the agent's softphone (by user_id), then bridge the lead."""
        body = _make_call_body(request, self._caller_id, self._status_callback)
        print(f"[exotel] make-call POST {self._ccm_base}/calls body={body}")
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as client:
            try:
                resp = await client.post(f"{self._ccm_base}/calls", json=body)
                resp.raise_for_status()
                rbody = resp.json()
            except httpx.HTTPStatusError as e:
                detail = e.response.text or str(e)
                return CallResponse(ok=False, error=f"exotel {e.response.status_code}: {detail}")
            except Exception as e:  # network / JSON
                return CallResponse(ok=False, error=str(e))

        data = _resp_data(rbody)
        return CallResponse(
            call_id=data.get("call_sid") or data.get("CallSid"),
            status=_STATUS_MAP.get((data.get("call_state") or "").strip().lower()),
            ok=True,
            provider_raw=rbody,
        )

    async def fetch_call_details(self, call_sid: str) -> Optional[dict]:
        """GET the Voice CDR by CallSid -> normalized `{status, duration_seconds,
        recording_url}`. Used for softphone calls, whose SDK reports no outcome and
        which fire no webhook. None if the CDR isn't ready/available."""
        if not call_sid:
            return None
        url = f"{self._calls_base}/{call_sid}.json"
        try:
            async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                body = resp.json()
        except Exception as e:
            logger.warning(f"[exotel] CDR fetch failed for {call_sid}: {e}")
            return None
        call = body.get("Call") if isinstance(body, dict) else None
        if not isinstance(call, dict):
            return None
        dur = str(call.get("Duration") or "").strip()
        return {
            "status": _norm_status(call.get("Status")),
            "duration_seconds": int(dur) if dur.isdigit() else None,
            "recording_url": (call.get("RecordingUrl") or "").strip() or None,
            "raw_status": call.get("Status"),
        }

    async def resolve_agent_email(self, agent_ref: str) -> Optional[str]:
        """Webhook agent ref -> the agent's CRM email (which matches a telecaller).
        CCM status_callback sends the Exotel `user_id` (reversed via the directory);
        inbound Passthru sends the agent's `sip:` id, reversed via the SIP map + the live
        usermapping cache (populated when that agent's softphone came online) — so inbound
        calls get attributed to the telecaller who took them, not logged as 'system'."""
        ref = (agent_ref or "").strip()
        if not ref:
            return None
        reverse_override = {v: k for k, v in self._email_overrides.items()}
        if ref.lower().startswith("sip:"):
            sip = ref.lower()
            exo = next((em for em, s in self._sip_map.items() if s == sip), None)
            if not exo:   # live usermapping cache: email -> (sip, ts)
                exo = next((em for em, (s, _ts) in self._sip_cache.items()
                            if (s or "").lower() == sip), None)
            return reverse_override.get(exo, exo) if exo else None
        directory = await self._agent_directory()          # email -> user_id
        email = next((em for em, uid in directory.items() if uid == ref), None)
        if not email:
            return None
        # reverse the CRM->Exotel override so the returned email is the agent's CRM login.
        return reverse_override.get(email, email)

    async def resolve_agent_dial(self, email: str) -> Optional[str]:
        """Telecaller email -> their Exotel `user_id`, which the CCM call API uses to
        ring that agent's softphone. Applies the CRM->Exotel email override for agents
        whose two emails differ. None if still unmatched."""
        if not email:
            return None
        e = email.strip().lower()
        e = self._email_overrides.get(e, e)
        return (await self._agent_directory()).get(e)

    async def resolve_agent_sip(self, email: str) -> Optional[str]:
        """Telecaller email -> their Exotel SIP id (e.g. `sip:naveenh37746fa6`), so an
        inbound call rings that agent's WebRTC softphone. Resolution order: CRM->Exotel
        email override, then the static `sip_map` (an explicit pin/override), then a live
        lookup against Exotel's `/usermapping` API (cached). None if the agent isn't
        provisioned -> the caller falls back to the agent's PSTN phone.

        The live lookup is what scales: no per-agent env entry needed — any agent
        provisioned in Exotel (i.e. whose softphone can register) resolves automatically.
        """
        if not email:
            return None
        e = email.strip().lower()
        e = self._email_overrides.get(e, e)
        if e in self._sip_map:           # explicit pin wins (no network)
            return self._sip_map[e]
        return await self._usermapping_sip(e)

    async def _usermapping_sip(self, exotel_email: str) -> Optional[str]:
        """Live `Data.SipId` from Exotel's /usermapping, cached per email (hits + misses).
        404 = agent not provisioned for the softphone -> None."""
        now = time.time()
        hit = self._sip_cache.get(exotel_email)
        if hit and now - hit[1] < self._sip_cache_ttl:
            return hit[0]
        sip = None
        token = await self._sdk_access_token()
        if token:
            try:
                # /usermapping builds ?user_id= unencoded downstream, so pre-encode '+'
                # (plus-addressed emails) to avoid it being read as a space (404).
                url = f"{self._usermapping_url}?user_id={exotel_email.replace('+', '%2B')}"
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    r = await client.get(url, headers={"Authorization": token})
                if r.status_code == 200:
                    sip = ((r.json() or {}).get("Data") or {}).get("SipId") or None
            except Exception as ex:
                logger.warning(f"[exotel] usermapping SIP lookup failed for {exotel_email}: {ex}")
        self._sip_cache[exotel_email] = (sip, now)
        return sip

    async def list_caller_ids(self) -> List[dict]:
        """ExoPhones on the account, scoped to the CRM flow (`crm_flow_id`). Cached
        (TTL) and serves the last good copy on failure — the list rarely changes."""
        now = time.time()
        if self._exo is not None and now - self._exo_ts < self._dir_ttl:
            return self._exo
        try:
            async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as client:
                resp = await client.get(self._exophones_url)
                resp.raise_for_status()
                body = resp.json()
            self._exo, self._exo_ts = _exophones_from_body(body, self._crm_flow_id), now
        except Exception as e:
            logger.warning(f"[exotel] exophone list fetch failed: {e}")
            if self._exo is None:
                self._exo, self._exo_ts = [], now
        return self._exo

    async def softphone_auth(self, email: str, name: str = "", agent_number: str = "") -> Optional[dict]:
        """{accessToken, userId} for the in-browser WebRTC SDK. userId is the agent's
        AppUserId (their email, override-applied — must match what's provisioned in
        Exotel's /usermapping). None until the app entity creds are configured.

        Lazily ensures the agent is MAPPED in Exotel (links their existing Exotel user
        to the app so their softphone can register) — see `_ensure_mapping`."""
        if not (self._app_id and self._app_secret and email):
            return None
        token = await self._sdk_access_token()
        if not token:
            return None
        e = email.strip().lower()
        exo = self._email_overrides.get(e, e)
        await self._ensure_mapping(exo, name, agent_number)
        # The CRM WebSDK builds `?user_id=${userId}` WITHOUT url-encoding, so a "+" in a
        # plus-addressed Exotel email (crm+1@…) is read as a space -> 404. Pre-encode it.
        return {"accessToken": token, "userId": exo.replace("+", "%2B")}

    async def _ensure_mapping(self, exotel_email: str, name: str, agent_number: str) -> None:
        """Lazy auto-map: if the agent has no /usermapping yet, LINK their existing Exotel
        user to the app (POST /usermapping). Never CREATES an Exotel user — guarded by the
        address-book directory, so an email that isn't already an Exotel user is skipped.
        Idempotent (GET-first) and attempted at most once per process per email."""
        if await self._usermapping_sip(exotel_email):
            return                                   # already mapped (cached GET)
        if exotel_email in self._provision_tried:
            return                                   # don't re-attempt this process
        # Guard: only map agents that already exist as Exotel users (the address book).
        directory = await self._agent_directory()    # email -> user_id (CCM users)
        if exotel_email not in directory:
            self._provision_tried.add(exotel_email)
            logger.info(f"[exotel] auto-map skipped: {exotel_email} is not an Exotel user "
                        "(align their CRM email to their Exotel email)")
            return
        # VirtualNumber for the mapping (shared default ExoPhone — outbound overrides it per call).
        exophones = await self.list_caller_ids()
        vn = exophones[0]["number"] if exophones else ""
        if not vn:
            logger.warning(f"[exotel] auto-map skipped: no ExoPhone for VirtualNumber ({exotel_email})")
            return
        self._provision_tried.add(exotel_email)
        token = await self._sdk_access_token()
        body = [{
            "AppUserId": exotel_email,
            "AppUsername": name or exotel_email.split("@")[0],
            "Email": exotel_email,
            "ExotelAccountSid": self._sid,
            "ExotelUserName": name or exotel_email.split("@")[0],
            "AgentNumber": agent_number or "",
            "VirtualNumber": vn,
        }]
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(self._usermapping_url, json=body,
                                      headers={"Authorization": token, "Content-Type": "application/json"})
            data = (r.json() or {}).get("Data") if r.status_code == 200 else None
            rec = data[0] if isinstance(data, list) and data else (data or {})
            sip = (rec or {}).get("SipId")
            if sip:
                self._sip_cache[exotel_email] = (sip, time.time())
                logger.info(f"[exotel] auto-mapped {exotel_email} -> {sip}")
            else:
                logger.warning(f"[exotel] auto-map POST for {exotel_email}: HTTP {r.status_code} {r.text[:200]}")
        except Exception as ex:
            logger.warning(f"[exotel] auto-map POST failed for {exotel_email}: {ex}")

    async def _sdk_access_token(self) -> Optional[str]:
        """Mint (and cache) the SDK 'app' access token. Auth is the Id/Secret in the
        body — NOT Basic api_key:api_token. Token is the response `Data` string."""
        if not (self._app_id and self._app_secret):
            return None   # no softphone creds -> nothing to mint (and don't hit the network)
        now = time.time()
        if self._sdk_token and now - self._sdk_token_ts < self._sdk_token_ttl:
            return self._sdk_token
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(self._token_url,
                                      json={"Id": self._app_id, "Secret": self._app_secret, "Entity": self._app_entity})
                r.raise_for_status()
                token = (r.json() or {}).get("Data")
            if token:
                self._sdk_token, self._sdk_token_ts = token, now
        except Exception as e:
            logger.warning(f"[exotel] softphone token mint failed: {e}")
        return self._sdk_token

    async def _agent_directory(self) -> dict:
        """Cached {email -> user_id} from the Users API. Serves the last good copy
        if a refresh fails, so a flaky API can't break click-to-call mid-shift."""
        now = time.time()
        if self._dir is not None and now - self._dir_ts < self._dir_ttl:
            return self._dir
        try:
            self._dir = await self._fetch_directory()
            self._dir_ts = now
        except Exception as e:
            logger.warning(f"[exotel] agent directory fetch failed: {e}")
            if self._dir is None:
                self._dir, self._dir_ts = {}, now   # avoid a refetch storm
        return self._dir

    async def _fetch_directory(self) -> dict:
        """Page through GET /users (the list is paginated, default 20/page) and build
        the email -> user_id map."""
        users: List[dict] = []
        offset = 0
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as client:
            while True:
                resp = await client.get(f"{self._ccm_base}/users?limit=50&offset={offset}")
                resp.raise_for_status()
                body = resp.json()
                page = _extract_users(body)
                users.extend(page)
                total = (body.get("metadata") or {}).get("total")
                offset += len(page)
                if not page or (isinstance(total, int) and offset >= total) or offset > 5000:
                    break
        out = _emails_to_userids(users)
        logger.info(f"[exotel] agent directory loaded: {len(out)} email->user_id entries")
        return out

    def _parse_ccm_event(self, call: Mapping[str, Any], ev: Mapping[str, Any],
                         payload: Mapping[str, Any]) -> CallEvent:
        """CCM StatusCallback — nested JSON `{event_details, call_details:{...}}` sent
        for OUTBOUND make-calls (different shape from the flat Passthru GET). Correlates
        via `custom_field` (the lead uid we set), and attributes via `user_id`."""
        agent = call.get("assigned_agent_details") or {}
        customer = call.get("customer_details") or {}
        ended = ((ev.get("event_type") or "").strip().lower() == "terminal"
                 or (call.get("call_state") or "").strip().lower() == "terminal")
        # On terminal, call_status is the real result; customer status is the fallback.
        status = _norm_status(call.get("call_status") or customer.get("status")
                              or call.get("call_state"))
        direction = (CallDirection.INBOUND if str(call.get("direction") or "").lower().startswith("in")
                     else CallDirection.OUTBOUND)
        dur = call.get("total_talk_time")
        if dur is None:
            dur = call.get("total_duration")
        rec = call.get("recordings")
        if isinstance(rec, list):
            rec = rec[0] if rec else None
        return CallEvent(
            kind=CallEventKind.ENDED if ended else CallEventKind.STARTED,
            call_id=str(call.get("call_sid") or ""),
            direction=direction,
            status=status,
            caller=_first(call, "virtual_number"),
            called=customer.get("contact_uri"),
            agent_number=agent.get("user_id") or agent.get("contact_uri"),
            telecaller_id=None,
            reference=_first(call, "custom_field"),
            duration_seconds=int(dur) if isinstance(dur, int)
            else (int(dur) if isinstance(dur, str) and dur.isdigit() else None),
            recording_url=rec if isinstance(rec, str) and rec else None,
            provider_raw=dict(payload),
        )

    def parse_event(self, payload: Mapping[str, Any]) -> CallEvent:
        """Normalize an Exotel call webhook (Passthru applet or StatusCallback) into a CallEvent."""
        call = payload.get("call_details")
        if isinstance(call, dict):   # nested CCM StatusCallback (outbound make-call)
            return self._parse_ccm_event(call, payload.get("event_details") or {}, payload)
        # Two webhook shapes hit this: API StatusCallback (From/To/Direction) and the
        # Passthru applet used in contact-centre call flows (CallFrom/CallTo, Direction
        # "incoming"). Coalesce both. Real status lives in DialCallStatus (completed)
        # with CallType (completed/incomplete) as the Passthru fallback; Status/CallStatus
        # appear on the API StatusCallback. _first skips the literal "null" Exotel sends.
        status = _norm_status(_first(payload, "Status", "CallStatus", "DialCallStatus", "CallType"))
        direction_raw = (_first(payload, "Direction") or "outbound").lower()
        direction = (CallDirection.INBOUND if direction_raw.startswith("in")  # inbound / incoming
                     else CallDirection.OUTBOUND)

        duration = _first(payload, "ConversationDuration", "DialCallDuration", "CallDuration")
        # Agent leg: Exotel sends a SIP id here (sip:naveenh37746fa6) we can't yet map
        # to a telecaller (see resolve_agent_email). Captured for the record regardless.
        agent_number = _first(payload, "DialWhomNumber", "AgentNumber")
        if not agent_number:
            agent_number = (_first(payload, "To", "CallTo") if direction == CallDirection.INBOUND
                            else _first(payload, "From", "CallFrom"))

        return CallEvent(
            kind=CallEventKind.ENDED if status in _TERMINAL else CallEventKind.STARTED,
            call_id=_first(payload, "CallSid", "Sid") or "",
            direction=direction,
            status=status,
            caller=_first(payload, "From", "CallFrom"),
            called=_first(payload, "To", "CallTo"),
            agent_number=agent_number,
            telecaller_id=None,
            duration_seconds=int(duration) if duration and duration.isdigit() else None,
            recording_url=_first(payload, "RecordingUrl"),
            provider_raw=dict(payload),
        )


if __name__ == "__main__":
    # ponytail: smoke check the pure paths — status, webhook normalization, parsing,
    # email->user_id, and the make-call body builder (the money path).
    assert _norm_status("No Answer") == CallStatus.NO_ANSWER
    assert _norm_status("completed") == CallStatus.COMPLETED
    assert _e164_in("9535328180") == "+919535328180"
    assert _e164_in("09535328180") == "+919535328180"
    assert _e164_in("+918068875264") == "+918068875264"

    a = ExotelAdapter("sid", "key", "token", caller_id="08047", status_callback="https://cb/exotel/call")

    # Real Passthru shapes seen in prod (contact-centre flow):
    pt = a.parse_event({
        "CallSid": "p1", "DialCallStatus": "completed", "CallType": "completed",
        "Direction": "incoming", "CallFrom": "08217264837", "CallTo": "08068875264",
        "From": "08217264837", "To": "08068875264", "DialWhomNumber": "sip:naveenh37746fa6",
        "DialCallDuration": "19", "RecordingUrl": "https://recordings/p1.mp3",
    })
    assert pt.kind == CallEventKind.ENDED and pt.status == CallStatus.COMPLETED
    assert pt.direction == CallDirection.INBOUND and pt.caller == "08217264837"
    assert pt.duration_seconds == 19

    # unanswered inbound — Exotel sends literal "null" strings; must NOT leak through.
    miss = a.parse_event({
        "CallSid": "p2", "DialCallStatus": "null", "CallType": "incomplete",
        "Direction": "incoming", "From": "08217264837", "To": "08068875264",
        "DialCallDuration": "0", "RecordingUrl": "null", "DialWhomNumber": "",
    })
    assert miss.status == CallStatus.NO_ANSWER and miss.recording_url is None

    # CCM StatusCallback (outbound make-call) — nested {event_details, call_details}.
    ccm = a.parse_event({
        "event_details": {"event_type": "terminal"},
        "call_details": {
            "call_sid": "c1", "direction": "outbound", "virtual_number": "+918068875144",
            "call_state": "terminal", "call_status": "customer_unanswered",
            "assigned_agent_details": {"user_id": "b240c0", "user_name": "Naveen Honne"},
            "customer_details": {"contact_uri": "+918073118693", "status": "no-answer"},
            "total_talk_time": 0, "total_duration": None, "recordings": None,
            "custom_field": "leads_6f3493c2-ba9d-4f3a-88ee-427c5d2371a5",
        },
    })
    assert ccm.kind == CallEventKind.ENDED and ccm.status == CallStatus.NO_ANSWER
    assert ccm.direction == CallDirection.OUTBOUND and ccm.called == "+918073118693"
    assert ccm.reference == "leads_6f3493c2-ba9d-4f3a-88ee-427c5d2371a5"
    assert ccm.agent_number == "b240c0" and ccm.duration_seconds == 0
    # the "answered" mid-call event is not terminal -> STARTED (no double-log).
    ccm_ans = a.parse_event({"event_details": {"event_type": "answered"},
                             "call_details": {"call_sid": "c1", "call_state": "active"}})
    assert ccm_ans.kind == CallEventKind.STARTED

    # Users API: real envelope is response[].data, each with email + id (no devices).
    users_body = {"metadata": {"total": 2, "count": 2, "offset": 0, "limit": 50}, "response": [
        {"code": 200, "status": "success", "data": {"id": "b240c0", "email": "CRM@silofortune.com"}},
        {"code": 200, "status": "success", "data": {"id": "9eae05", "email": "shreya.m@silofortune.com"}},
    ]}
    dir_ = _emails_to_userids(_extract_users(users_body))
    assert dir_ == {"crm@silofortune.com": "b240c0", "shreya.m@silofortune.com": "9eae05"}

    import asyncio
    a._dir, a._dir_ts = dir_, time.time()
    a._email_overrides = _parse_overrides("Naveen.H@silofortune.com:CRM@silofortune.com")
    assert asyncio.run(a.resolve_agent_dial("crm@silofortune.com")) == "b240c0"   # direct email -> user_id
    assert asyncio.run(a.resolve_agent_dial("naveen.h@silofortune.com")) == "b240c0"  # via override
    assert asyncio.run(a.resolve_agent_dial("nobody@x.com")) is None
    # email -> SIP for inbound softphone routing (via the configured map + override).
    a._sip_map = _parse_overrides("crm@silofortune.com:sip:naveenh37746fa6")
    assert asyncio.run(a.resolve_agent_sip("crm@silofortune.com")) == "sip:naveenh37746fa6"
    assert asyncio.run(a.resolve_agent_sip("naveen.h@silofortune.com")) == "sip:naveenh37746fa6"  # via override
    assert asyncio.run(a.resolve_agent_sip("nobody@x.com")) is None
    # SIP reversed back to the agent's CRM email (via sip_map + override) for inbound attribution.
    assert asyncio.run(a.resolve_agent_email("sip:naveenh37746fa6")) == "naveen.h@silofortune.com"
    assert asyncio.run(a.resolve_agent_email("sip:unknown")) is None
    # CCM user_id -> CRM email (directory reverse + override reverse), for attribution.
    assert asyncio.run(a.resolve_agent_email("b240c0")) == "naveen.h@silofortune.com"
    assert asyncio.run(a.resolve_agent_email("nope")) is None

    # Make-call body: from=user_id, E.164 to/virtual, status_callback array.
    body = _make_call_body(
        CallRequest(agent_number="b240c0", lead_number="9535328180",
                    caller_id="+918068875264", reference="leads_x"),
        "08047", "https://cb/exotel/call")
    assert body["from"] == {"user_id": "b240c0"}
    assert body["to"] == {"customer_contact_uri": "+919535328180"} and body["virtual_number"] == "+918068875264"
    assert body["custom_field"] == "leads_x"
    assert {e["event"] for e in body["status_callback"]} == {"terminal", "answered"}

    # ExoPhone list, scoped to the CRM flow via voice_url.
    phones_body = {"incoming_phone_numbers": [
        {"phone_number": "08068875264", "friendly_name": "CRM-1",
         "voice_url": "http://my.exotel.in/Exotel/exoml/start_voice/45195"},
        {"phone_number": "08047332571", "friendly_name": "LSQ-1",
         "voice_url": "http://my.exotel.in/Exotel/exoml/start_voice/99999"},
    ]}
    assert _exophones_from_body(phones_body, "45195") == [{"number": "08068875264", "label": "CRM-1"}]
    assert len(_exophones_from_body(phones_body, "")) == 2
    print("exotel adapter OK")
