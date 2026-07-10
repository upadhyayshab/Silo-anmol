"""ExotelAdapter — the only place Exotel-specific HTTP/field logic lives.

Implements the `TelephonyProvider` port for the WebRTC-softphone-only setup:
  - `softphone_auth`      -> mint SDK credentials + lazy auto-map (/usermapping).
  - `resolve_agent_sip`   -> agent email -> SIP id (inbound rings the softphone).
  - `resolve_agent_email` -> reverse mapping, for webhook agent attribution.
  - `agent_device_status` -> bulk softphone health for the admin view.
  - `fetch_call_details`  -> Voice CDR (softphone calls fire no webhook).
  - `parse_event`         -> normalize the call webhook (Passthru / StatusCallback).

Auth is HTTP Basic (api_key:api_token). The CCM APIs (users) live on the
`ccm-api.*` host; voice/ExoPhone APIs on the `api.*` host. ExoPhones are still
listed (`list_caller_ids`, internal) solely as the VirtualNumber for auto-map.
Docs:
  https://developer.exotel.com/api/users
  https://support.exotel.com/support/solutions/articles/48283-working-with-passthru-applet

Note: this account's Users API does NOT expose agents' SIP/device ids in the
address book, so SIP ids come from /usermapping — see `resolve_agent_sip`.
"""
import logging
import re
import time
from typing import Any, List, Mapping, Optional

import httpx

from core.telephony import (
    CallDirection, CallEvent, CallEventKind, CallStatus,
    TelephonyProvider,
)
from utils.dedup_utils import normalize_mobile

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


def _exophones_with_flow(body: Any) -> List[dict]:
    """EVERY ExoPhone on the account + the call-flow id its `voice_url` points at.
    Unlike `_exophones_from_body` this does NOT filter by flow — the admin picker must
    show numbers that aren't on the CRM flow yet (a caller-ID with no inbound flow means
    customer callbacks to it never reach the CRM)."""
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
        m = re.search(r"start_voice/(\d+)", str(r.get("voice_url") or ""))
        out.append({"number": number,
                    "label": r.get("friendly_name") or number,
                    "flow_id": m.group(1) if m else None})
    return out


def _usermappings_from_body(body: Any) -> List[dict]:
    """`Data.Users[]` from the bulk /usermapping read, with **SipSecret removed** — the
    payload carries it and it must never reach a browser."""
    users = ((body or {}).get("Data") or {}).get("Users") or []
    return [{k: v for k, v in u.items() if k != "SipSecret"}
            for u in users if isinstance(u, dict)]


def _put_body(rec: Mapping[str, Any], vn: str) -> dict:
    """Body for PUT /usermapping. It takes a **single object**; an array is rejected with
    `400 invalid Payload` (POST, confusingly, takes an array). Undocumented."""
    uid = rec.get("AppUserId")
    return {
        "AppUserId": uid,
        "AppUsername": rec.get("AppUsername") or "",
        "Email": rec.get("Email") or uid,
        "ExotelAccountSid": rec.get("ExotelAccountSid") or "",
        "ExotelUserName": rec.get("ExotelUserName") or "",
        "AgentNumber": rec.get("AgentNumber") or "",
        "VirtualNumber": vn,
    }


def _needs_vn_update(current: Optional[str], desired: Optional[str]) -> bool:
    """Compare NORMALIZED numbers. Exotel stores the same ExoPhone as '08068875144' and
    '+918068875144'; a raw compare would PUT on every softphone open (each PUT rotates
    the agent's SipSecret)."""
    if not desired:
        return False
    return normalize_mobile(current) != normalize_mobile(desired)


async def _choose_vn(adapter, virtual_number: str) -> str:
    """The VirtualNumber to provision a NEW mapping with. Prefer the caller-supplied
    (state-derived) number; fall back to the first flow-scoped ExoPhone.

    ponytail: the `exophones[0]` fallback is the old lottery — its order is Exotel's,
    which is exactly how 109 agents ended up split 83/26 across two numbers. It survives
    only for accounts with no config; once `default_exophone` is set it never runs.
    """
    if virtual_number:
        return virtual_number
    exophones = await adapter.list_caller_ids()
    return exophones[0]["number"] if exophones else ""


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
                 subdomain: str = "api.exotel.com",
                 ccm_subdomain: str = "ccm-api.exotel.com",
                 crm_flow_id: str = "", email_overrides: str = "", sip_map: str = "",
                 app_id: str = "", app_secret: str = "", app_entity: str = "app",
                 integrations_host: str = "integrationscore.mum1.exotel.com"):
        self._sid = sid
        self._auth = (api_key, api_token)
        self._exophones_url = f"https://{subdomain}/v2_beta/Accounts/{sid}/IncomingPhoneNumbers"
        self._calls_base = f"https://{subdomain}/v1/Accounts/{sid}/Calls"  # Voice CDR (call details)
        self._ccm_base = f"https://{ccm_subdomain}/v2/accounts/{sid}"  # Users + Calls (CCM) APIs
        self._crm_flow_id = str(crm_flow_id)  # only list ExoPhones whose flow is the CRM app
        self._email_overrides = _parse_overrides(email_overrides)  # CRM email -> Exotel email exceptions
        # ...and back, so nothing outside this adapter ever handles an Exotel-side email.
        self._reverse_email_overrides = {v: k for k, v in self._email_overrides.items()}
        self._sip_map = _parse_overrides(sip_map)  # Exotel email -> SIP id, for inbound softphone routing
        # WebRTC softphone SDK auth — App-entity Id/Secret (IP-PSTN-intermix onboarding).
        self._app_id = app_id
        self._app_secret = app_secret
        self._app_entity = app_entity or "app"   # token scope sent to Exotel as Entity
        self._token_url = f"https://{integrations_host}/v2/integrations/token"
        self._usermapping_url = f"https://{integrations_host}/v2/integrations/usermapping"
        self._device_url = f"https://{integrations_host}/v2/integrations/device"
        self._app_setting_url = f"https://{integrations_host}/v2/integrations/app_setting"
        self._app_settings_done = False   # push _REQUIRED_APP_SETTINGS once per process
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
        # Per-agent devices (CCM Users ?fields=devices), cached — softphone capability + verified.
        self._dev: Optional[dict] = None
        self._dev_ts = 0.0

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

    async def agent_device_status(self, emails: List[str]) -> dict:
        """{crm_email_lower: {'sip', 'verified'}} from ONE paged CCM Users
        `?fields=devices` call (cached). `sip` None = no softphone (the agent cannot
        call — the WebRTC softphone is the only path). `verified` False = the
        unverified-`tel` Primary-device pattern that keeps an unusable device active
        outbound (10725); True = healthy; None when no SIP."""
        devmap = await self._agent_devices()
        out: dict = {}
        for e in {(x or "").strip().lower() for x in emails if (x or "").strip()}:
            exo = self._email_overrides.get(e, e)
            d = devmap.get(exo) or devmap.get(e)
            if not d or not d.get("has_sip"):
                out[e] = {"sip": None, "verified": None}
            else:
                # broken = an unverified tel device stays active outbound -> 10725.
                verified = (not d.get("has_tel")) or bool(d.get("tel_verified"))
                out[e] = {"sip": d.get("sip"), "verified": verified}
        return out

    async def _agent_devices(self) -> dict:
        """Cached {exotel_email -> {sip, has_tel, tel_verified, sip_verified}} from the CCM
        Users API. Serves the last good copy if a refresh fails, like the agent directory."""
        now = time.time()
        if self._dev is not None and now - self._dev_ts < self._dir_ttl:
            return self._dev
        try:
            self._dev = await self._fetch_agent_devices()
            self._dev_ts = now
        except Exception as e:
            logger.warning(f"[exotel] agent devices fetch failed: {e}")
            if self._dev is None:
                self._dev, self._dev_ts = {}, now
        return self._dev

    async def _fetch_agent_devices(self) -> dict:
        """Page GET /users?fields=devices and pull each agent's sip/tel device state
        (type, verified, contact_uri). One bulk read for the whole team — same shape the
        exotel_unverified_report.py audit uses."""
        out: dict = {}
        offset = 0
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as client:
            while True:
                resp = await client.get(
                    f"{self._ccm_base}/users?fields=devices&limit=50&offset={offset}")
                resp.raise_for_status()
                body = resp.json()
                rows = body.get("response") or []
                for row in rows:
                    d = (row or {}).get("data") or {}
                    email = (d.get("email") or "").strip().lower()
                    if not email:
                        continue
                    devs = d.get("devices") or []
                    tel = next((x for x in devs if x.get("type") == "tel"), None)
                    sipd = next((x for x in devs if x.get("type") == "sip"), None)
                    out[email] = {
                        "has_sip": sipd is not None,
                        "sip": (sipd or {}).get("contact_uri"),
                        "has_tel": tel is not None,
                        "tel_verified": bool(tel and tel.get("verified")),
                        "sip_verified": bool(sipd and sipd.get("verified")),
                    }
                total = (body.get("metadata") or {}).get("total")
                offset += len(rows)
                if not rows or (isinstance(total, int) and offset >= total) or offset > 5000:
                    break
        logger.info(f"[exotel] agent devices loaded: {len(out)} users")
        return out

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

    # Exotel's bulk /usermapping read silently returns 20 rows unless page_size is
    # passed; limit/offset/count are accepted and ignored. Undocumented.
    _USERMAPPING_PAGE_SIZE = 500

    def _to_exotel_email(self, email: str) -> str:
        """CRM email -> Exotel email. Idempotent: an already-Exotel address isn't a key."""
        e = (email or "").strip().lower()
        return self._email_overrides.get(e, e)

    def _to_crm_email(self, exotel_email: str) -> str:
        """Exotel email -> CRM email, so callers outside the adapter only see CRM ones."""
        e = (exotel_email or "").strip().lower()
        return self._reverse_email_overrides.get(e, e)

    async def list_caller_ids_all(self) -> List[dict]:
        """Every ExoPhone on the account (NOT flow-filtered) with its `flow_id`, for the
        admin state->ExoPhone picker."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as client:
                resp = await client.get(self._exophones_url)
                resp.raise_for_status()
                return _exophones_with_flow(resp.json())
        except Exception as e:
            logger.warning(f"[exotel] exophone (all) fetch failed: {e}")
            return []

    async def list_usermappings(self) -> List[dict]:
        """Every app usermapping in one call, SipSecret stripped, `AppUserId` translated
        back to the CRM email. MUST send `page_size` — without it Exotel returns 20 rows
        and no hint that it truncated."""
        token = await self._sdk_access_token()
        if not token:
            return []
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(self._usermapping_url,
                                     params={"page_size": self._USERMAPPING_PAGE_SIZE},
                                     headers={"Authorization": token})
                r.raise_for_status()
                rows = _usermappings_from_body(r.json())
        except Exception as e:
            logger.warning(f"[exotel] usermapping list failed: {e}")
            return []
        for rec in rows:
            rec["AppUserId"] = self._to_crm_email(rec.get("AppUserId") or "")
        return rows

    async def usermapping_record(self, email: str) -> Optional[dict]:
        """One agent's full mapping record (by CRM email), or None when unmapped (404)."""
        token = await self._sdk_access_token()
        if not token:
            return None
        exo = self._to_exotel_email(email)
        try:
            # /usermapping builds ?user_id= unencoded downstream, so pre-encode '+'.
            url = f"{self._usermapping_url}?user_id={exo.replace('+', '%2B')}"
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url, headers={"Authorization": token})
            if r.status_code != 200:
                return None
            data = (r.json() or {}).get("Data")
            rec = data[0] if isinstance(data, list) and data else (data or {})
            return rec or None
        except Exception as e:
            logger.warning(f"[exotel] usermapping read failed for {exo}: {e}")
            return None

    async def set_virtual_number(self, email: str, vn: str,
                                 rec: Optional[dict] = None) -> bool:
        """Re-point an existing mapping's outbound caller-ID (agent given by CRM email).

        PUT updates in place: SipId and device ids stay stable (verified 2026-07-09), so
        inbound SIP routing is unaffected. It DOES rotate SipSecret — harmless to the
        WebRTC SDK (which auths with accessToken+userId), but a reason to only call this
        on genuine drift.
        """
        if not vn:
            return False
        token = await self._sdk_access_token()
        if not token:
            return False
        rec = rec or await self.usermapping_record(email)
        if not rec:
            return False
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.put(self._usermapping_url, json=_put_body(rec, vn),
                                     headers={"Authorization": token,
                                              "Content-Type": "application/json"})
            if r.status_code == 200:
                logger.info(f"[exotel] caller-id {email} -> {vn}")
                return True
            logger.warning(f"[exotel] set VirtualNumber {email}: "
                           f"HTTP {r.status_code} {r.text[:200]}")
        except Exception as e:
            logger.warning(f"[exotel] set VirtualNumber failed for {email}: {e}")
        return False

    async def _sync_virtual_number(self, exotel_email: str, desired: str) -> None:
        """Re-point an ALREADY-mapped agent's caller-ID when config changed.

        `_ensure_mapping` returns early for mapped agents, so without this an agent would
        keep their first-ever VirtualNumber forever. Only PUTs on genuine drift — each PUT
        rotates SipSecret, and a raw compare would fire on every softphone open because
        Exotel stores '08068875144' and '+918068875144' interchangeably.
        """
        if not desired:
            return
        rec = await self.usermapping_record(exotel_email)
        if not rec or not _needs_vn_update(rec.get("VirtualNumber"), desired):
            return
        await self.set_virtual_number(exotel_email, desired, rec=rec)

    async def softphone_auth(self, email: str, name: str = "", agent_number: str = "",
                             virtual_number: str = "") -> Optional[dict]:
        """{accessToken, userId} for the in-browser WebRTC SDK. userId is the agent's
        AppUserId (their email, override-applied — must match what's provisioned in
        Exotel's /usermapping). None until the app entity creds are configured.

        `virtual_number` is the outbound caller-ID this agent should present (their state's
        ExoPhone). It provisions a new mapping and re-points an existing one on drift.

        Lazily ensures the agent is MAPPED in Exotel (links their existing Exotel user
        to the app so their softphone can register) — see `_ensure_mapping`."""
        if not (self._app_id and self._app_secret and email):
            return None
        token = await self._sdk_access_token()
        if not token:
            return None
        await self._ensure_app_settings()   # pin record=true (once per process); best-effort
        e = email.strip().lower()
        exo = self._email_overrides.get(e, e)
        await self._ensure_mapping(exo, name, agent_number, virtual_number)
        await self._sync_virtual_number(exo, virtual_number)   # config changed -> re-point
        # Flip the agent's softphone (SIP) device Available — the same flag as the ON/OFF
        # toggle in the Exotel dashboard. Without it, CCM make-call fails 10708 ("no online
        # device") even though the SDK's SIP is registered. Best-effort: never blocks auth.
        await self.set_device_status(exo, on=True)
        # The CRM WebSDK builds `?user_id=${userId}` WITHOUT url-encoding, so a "+" in a
        # plus-addressed Exotel email (crm+1@…) is read as a space -> 404. Pre-encode it.
        return {"accessToken": token, "userId": exo.replace("+", "%2B")}

    async def _ensure_mapping(self, exotel_email: str, name: str, agent_number: str,
                              virtual_number: str = "") -> None:
        """Lazy auto-map: if the agent has no /usermapping yet, LINK their existing Exotel
        user to the app (POST /usermapping). Never CREATES an Exotel user — guarded by the
        address-book directory, so an email that isn't already an Exotel user is skipped.
        Idempotent (GET-first) and attempted at most once per process per email.

        `virtual_number` is the agent's state ExoPhone — the caller-ID the new mapping is
        provisioned with. Already-mapped agents return early here; `_sync_virtual_number`
        is what re-points them."""
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
        # Outbound caller-ID for the new mapping: the agent's state ExoPhone when configured.
        vn = await _choose_vn(self, virtual_number)
        if not vn:
            logger.warning(f"[exotel] auto-map skipped: no ExoPhone for VirtualNumber ({exotel_email})")
            return
        self._provision_tried.add(exotel_email)
        await self._post_usermapping(exotel_email, name, agent_number, vn)

    async def _post_usermapping(self, exotel_email: str, name: str, agent_number: str,
                                vn: str) -> Optional[str]:
        """POST /usermapping -> the agent's new SipId (None on failure). Creates the Exotel
        user when it doesn't exist, provisions their SIP device, and mints the mapping.
        The body is an **array** (PUT, confusingly, takes a single object)."""
        token = await self._sdk_access_token()
        if not token:
            return None
        display = name or exotel_email.split("@")[0]
        body = [{
            "AppUserId": exotel_email,
            "AppUsername": display,
            "Email": exotel_email,
            "ExotelAccountSid": self._sid,
            "ExotelUserName": display,
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
                self._dev_ts = 0.0   # the devices cache no longer knows about this agent
                logger.info(f"[exotel] mapped {exotel_email} -> {sip} (caller-id {vn})")
                return sip
            logger.warning(f"[exotel] usermapping POST for {exotel_email}: HTTP {r.status_code} {r.text[:200]}")
        except Exception as ex:
            logger.warning(f"[exotel] usermapping POST failed for {exotel_email}: {ex}")
        return None

    async def provision_user(self, email: str, name: str = "", agent_number: str = "",
                             virtual_number: str = "") -> Optional[str]:
        """CREATE the Exotel user + softphone mapping for an agent who has none, and return
        their SipId. This is the one path allowed to create — `_ensure_mapping` only ever
        links agents that already exist in the address book, so agency telecallers (who
        never do) can't be onboarded lazily.

        Idempotent: an already-mapped agent's existing SipId is returned without a write.
        `virtual_number` is their state's ExoPhone; falls back to the flow-scoped first."""
        if not email:
            return None
        exo = self._to_exotel_email(email)
        existing = await self._usermapping_sip(exo)
        if existing:
            return existing
        vn = await _choose_vn(self, virtual_number)
        if not vn:
            logger.warning(f"[exotel] provision skipped: no ExoPhone for VirtualNumber ({exo})")
            return None
        sip = await self._post_usermapping(exo, name, agent_number, vn)
        if sip:
            self._dir_ts = 0.0             # a brand-new Exotel user; refresh the address book
            self._provision_tried.discard(exo)
        return sip

    # App-LEVEL (per AppID, not per-user) integration settings we require. `record=true` so
    # Exotel records softphone calls — it was missing and had to be set by hand (Exotel ticket
    # 2026-07-08); pinned here so a re-provisioned app doesn't silently lose recordings again.
    _REQUIRED_APP_SETTINGS = {"record": "true"}

    async def _ensure_app_settings(self) -> None:
        """Idempotently push _REQUIRED_APP_SETTINGS via POST /v2/integrations/app_setting.
        App-level, so once per process is enough; best-effort — never blocks softphone auth.
        Leaves the done-flag unset if it couldn't run (creds/token not ready, or any POST
        failed), so a later softphone open retries."""
        if self._app_settings_done or not (self._app_id and self._app_secret):
            return
        token = await self._sdk_access_token()
        if not token:
            return   # creds not ready yet -> retry on a later auth
        ok = True
        for key, value in self._REQUIRED_APP_SETTINGS.items():
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    r = await client.post(
                        self._app_setting_url, json={"Key": key, "Value": value},
                        headers={"Authorization": token, "Content-Type": "application/json"})
                if r.status_code == 200:
                    logger.info(f"[exotel] app_setting {key}={value} ensured")
                else:
                    ok = False
                    logger.warning(f"[exotel] app_setting {key} POST: HTTP {r.status_code} {r.text[:200]}")
            except Exception as ex:
                ok = False
                logger.warning(f"[exotel] app_setting {key} POST failed: {ex}")
        self._app_settings_done = ok   # retry on a later auth if any setting failed

    async def set_device_status(self, email: str, on: bool = True, device: str = "sip") -> bool:
        """Flip an agent's Exotel device availability via PUT /v2/integrations/device — the
        same flag as the dashboard's ON/OFF toggle. The WebSDK only REGISTERS the SIP device;
        this availability flag is separate, and CCM make-call needs it ON or it errors 10708.

        `device`: "sip" (the WebRTC softphone) or "phone" (the agent's PSTN leg). UserId is
        the AppUserId (agent's Exotel email), matching /usermapping. Best-effort: returns
        False (and logs) on any failure — never blocks the softphone token mint or a call.
        """
        if not email:
            return False
        token = await self._sdk_access_token()
        if not token:
            return False
        e = email.strip().lower()
        exo = self._email_overrides.get(e, e)
        body = {"UserId": exo, "DeviceName": device, "DeviceStatus": bool(on)}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.put(self._device_url, json=body,
                                     headers={"Authorization": token, "Content-Type": "application/json"})
            if r.status_code == 200:
                return True
            logger.warning(f"[exotel] set device {device}={on} for {exo}: HTTP {r.status_code} {r.text[:200]}")
        except Exception as ex:
            logger.warning(f"[exotel] set device status failed for {exo}: {ex}")
        return False

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
    # and the email->user_id / email->SIP mappings.
    assert _norm_status("No Answer") == CallStatus.NO_ANSWER
    assert _norm_status("completed") == CallStatus.COMPLETED

    a = ExotelAdapter("sid", "key", "token")
    assert a._device_url.endswith("/v2/integrations/device")   # device-status PUT target wired

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

    # ExoPhone list, scoped to the CRM flow via voice_url (auto-map VirtualNumber source).
    phones_body = {"incoming_phone_numbers": [
        {"phone_number": "08068875264", "friendly_name": "CRM-1",
         "voice_url": "http://my.exotel.in/Exotel/exoml/start_voice/45195"},
        {"phone_number": "08047332571", "friendly_name": "LSQ-1",
         "voice_url": "http://my.exotel.in/Exotel/exoml/start_voice/99999"},
    ]}
    assert _exophones_from_body(phones_body, "45195") == [{"number": "08068875264", "label": "CRM-1"}]
    assert len(_exophones_from_body(phones_body, "")) == 2

    # provision_user: creates only when unmapped, is idempotent, and applies the email
    # override + the state's caller-ID. Stubs the two network edges.
    p = ExotelAdapter("sid", "key", "token", app_id="i", app_secret="s",
                      email_overrides="agency.rm@x.com:rm@exotel.com")
    posted = []

    async def _fake_post(exo, name, num, vn):
        posted.append((exo, name, num, vn))
        return "sip:newagent01"

    p._post_usermapping = _fake_post
    p._sip_cache = {"mapped@x.com": ("sip:already", time.time())}

    # already mapped -> returns the existing SIP, writes nothing.
    assert asyncio.run(p.provision_user("mapped@x.com")) == "sip:already"
    assert posted == []
    # unmapped -> creates, with the override applied and the state's ExoPhone as caller-ID.
    p._sip_cache["rm@exotel.com"] = (None, time.time())          # cached 404 = unmapped
    sip = asyncio.run(p.provision_user("Agency.RM@x.com", name="RM One",
                                       agent_number="9876543210", virtual_number="08068875264"))
    assert sip == "sip:newagent01"
    assert posted == [("rm@exotel.com", "RM One", "9876543210", "08068875264")]
    # no caller-ID anywhere -> refuses rather than guessing.
    p._sip_cache["fresh@x.com"] = (None, time.time())
    p._exo, p._exo_ts = [], time.time()                          # no ExoPhones on the account
    assert asyncio.run(p.provision_user("fresh@x.com")) is None
    assert len(posted) == 1                                      # nothing posted
    print("exotel adapter OK")
