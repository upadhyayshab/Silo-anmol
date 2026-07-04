"""Telephony port — the vendor-agnostic interface the CRM core depends on.

Two backend jobs, both vendor-agnostic via this port:
  - `connect_call`  -> place an outbound click-to-call (agent dialed first, then
                       the lead is bridged). Exotel does this with a server API.
  - `parse_event`   -> normalize a call webhook (start/end + recording) so the CRM
                       can log the call onto the lead's timeline.

Inbound owner-based routing (Feature 3.2) and agent presence (3.3) are *not* here
yet — they need a `telecaller_status` table and round-robin logic that lives in
the CRM service, not the adapter. Add when that engine is built.

The concrete `ExotelAdapter(TelephonyProvider)` is wired by
`dependencies/telephony_dep.py`; the webhook router calls
`services/telephonyService.handle_event`, the click-to-call route calls
`connect_call`. Swap a future vendor in one place without touching the CRM.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional

from pydantic import BaseModel


class CallDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(str, Enum):
    """Normalized call status — each adapter maps its vendor's codes onto this
    set so the rest of the CRM sees one vocabulary."""
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    CANCELED = "canceled"


class CallEventKind(str, Enum):
    STARTED = "started"   # call connected
    ENDED = "ended"       # call finished -> log outcome + recording


class CallEvent(BaseModel):
    """Normalized call-start / call-end / post-call webhook from the dialer."""
    kind: CallEventKind
    call_id: str
    direction: CallDirection
    status: CallStatus
    caller: Optional[str] = None
    called: Optional[str] = None
    agent_number: Optional[str] = None    # which agent took the call (from the webhook)
    telecaller_id: Optional[str] = None   # set if the vendor sends an agent id we map
    reference: Optional[str] = None       # our custom_field echoed back (lead uid) to correlate
    duration_seconds: Optional[int] = None
    recording_url: Optional[str] = None
    occurred_at: Optional[datetime] = None
    provider_raw: Optional[dict] = None


class CallRequest(BaseModel):
    """Click-to-call: dial `agent_number` first, then bridge `lead_number`."""
    agent_number: str
    lead_number: str
    caller_id: Optional[str] = None    # override the adapter's default DID, if needed
    reference: Optional[str] = None    # echoed back on webhooks to correlate (e.g. lead uid)


class CallResponse(BaseModel):
    """What the dialer returned when we asked it to place the call."""
    call_id: Optional[str] = None
    status: Optional[CallStatus] = None
    ok: bool = True
    error: Optional[str] = None
    provider_raw: Optional[dict] = None


class TelephonyProvider(ABC):
    """Port: the only telephony contract the CRM core knows about."""

    @abstractmethod
    async def connect_call(self, request: CallRequest) -> CallResponse:
        """Place an outbound click-to-call (agent-first, then bridge the lead)."""

    @abstractmethod
    def parse_event(self, payload: Mapping[str, Any]) -> CallEvent:
        """Normalize a vendor call webhook (start / end / post-call) into a CallEvent."""

    async def resolve_agent_email(self, agent_ref: str) -> Optional[str]:
        """Map a vendor agent reference (e.g. a SIP id) to the agent's email — the
        CRM then matches that email to a telecaller. Default: unsupported (returns
        None); adapters whose webhook agent id isn't a phone override this."""
        return None

    async def resolve_agent_dial(self, email: str) -> Optional[str]:
        """Reverse of the above: a telecaller's email -> the leg to dial them on for
        outbound click-to-call (e.g. their SIP softphone id), so the call rings the
        agent's app. Default: unsupported (returns None); the caller then falls back."""
        return None

    async def resolve_agent_sip(self, email: str) -> Optional[str]:
        """A telecaller's email -> their SIP id (e.g. `sip:naveenh37746fa6`) so an
        INBOUND call can be routed to that agent's WebRTC softphone instead of their
        PSTN phone. Default: unsupported (returns None); the caller then falls back to
        the agent's phone number."""
        return None

    async def list_caller_ids(self) -> list:
        """The caller-ID numbers (DIDs) the CRM may dial out from, each as
        `{"number": ..., "label": ...}`. The adapter scopes these to the CRM's own
        call flow so unrelated numbers don't show. Default: none."""
        return []

    async def agent_device_status(self, emails: list) -> dict:
        """Map each agent email -> `{sip, mode, verified}`: which call path they get and
        whether their device is verified. `mode` is 'softphone' (has a SIP, in-browser
        WebRTC) or 'ssc' (server-side call); `verified` flags softphone outbound health
        (None when n/a). Display-only for the admin 'who gets what' view. Default: none."""
        return {}

    async def softphone_auth(self, email: str, name: str = "", agent_number: str = "") -> Optional[dict]:
        """Credentials for an in-browser softphone SDK: `{accessToken, userId}` for
        the given agent, or None if the vendor has no embeddable softphone / it isn't
        configured. The CRM frontend inits the SDK with these. `name`/`agent_number`
        let the adapter lazily map the agent on first use."""
        return None

    async def fetch_call_details(self, call_sid: str) -> Optional[dict]:
        """Pull a finished call's record by its id — `{status: CallStatus,
        duration_seconds, recording_url}` — for cases where no webhook fires (e.g. the
        in-browser softphone, whose SDK reports no outcome). None if unsupported/unavailable."""
        return None


class MockTelephonyProvider(TelephonyProvider):
    """In-memory stand-in so the webhook route can be built and tested before the
    real `RadicalMindsAdapter` lands. Field guesses below are just so the mock
    accepts a generic payload in tests — the RM adapter maps RM's real fields.

    ponytail: 'null adapter' to unblock dev, not a vendor.
    """

    async def connect_call(self, request: CallRequest) -> CallResponse:
        return CallResponse(call_id="mock-call", status=CallStatus.RINGING)

    def parse_event(self, payload: Mapping[str, Any]) -> CallEvent:
        return CallEvent(
            kind=CallEventKind(payload.get("kind", "ended")),
            call_id=str(payload.get("call_id") or payload.get("CallSid") or ""),
            direction=CallDirection(payload.get("direction", "inbound")),
            status=CallStatus(payload.get("status", "completed")),
            caller=payload.get("caller"),
            called=payload.get("called"),
            agent_number=payload.get("agent_number"),
            telecaller_id=payload.get("telecaller_id"),
            duration_seconds=payload.get("duration_seconds"),
            recording_url=payload.get("recording_url"),
            provider_raw=dict(payload),
        )


__all__ = [
    "CallDirection", "CallStatus", "CallEventKind",
    "CallEvent", "CallRequest", "CallResponse",
    "TelephonyProvider", "MockTelephonyProvider",
]


if __name__ == "__main__":
    # ponytail: smoke check — the ABC is implementable and the DTO round-trips.
    m = MockTelephonyProvider()
    ev = m.parse_event({
        "kind": "ended", "call_id": "abc", "status": "no_answer",
        "direction": "inbound", "caller": "+919812345678",
        "duration_seconds": 0, "recording_url": "https://rec/abc.mp3",
    })
    assert ev.kind == CallEventKind.ENDED and ev.status == CallStatus.NO_ANSWER
    assert ev.call_id == "abc" and ev.recording_url.endswith("abc.mp3")
    print("telephony port OK")
