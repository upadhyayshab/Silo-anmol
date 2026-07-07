"""Telephony port — the vendor-agnostic interface the CRM core depends on.

The in-browser WebRTC softphone is the primary call path. The port covers what
the CRM core needs from a vendor:
  - `parse_event`        -> normalize a call webhook (start/end + recording) so the
                            CRM can log the call onto the lead's timeline.
  - `resolve_agent_sip`  -> agent email -> SIP id, so inbound rings the softphone.
  - `softphone_auth`     -> mint the in-browser softphone SDK credentials.
  - `fetch_call_details` -> CDR lookup for softphone calls (they fire no webhook).
  - `connect_call`       -> server-side click-to-call (SSC). Only reachable when the
                            `exotel_ssc_fallback` toggle is on — off = WebRTC-only.

The concrete `ExotelAdapter(TelephonyProvider)` is wired by
`dependencies/telephony_dep.py`; the webhook router calls
`services/telephonyService.handle_event`. Swap a future vendor in one place
without touching the CRM.
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
        """Place an outbound click-to-call (agent-first, then bridge the lead).
        SSC fallback path — the route calling this is gated by `exotel_ssc_fallback`."""

    @abstractmethod
    def parse_event(self, payload: Mapping[str, Any]) -> CallEvent:
        """Normalize a vendor call webhook (start / end / post-call) into a CallEvent."""

    async def resolve_agent_email(self, agent_ref: str) -> Optional[str]:
        """Map a vendor agent reference (e.g. a SIP id) to the agent's email — the
        CRM then matches that email to a telecaller. Default: unsupported (returns
        None); adapters whose webhook agent id isn't a phone override this."""
        return None

    async def resolve_agent_dial(self, email: str) -> Optional[str]:
        """A telecaller's email -> the leg to dial them on for outbound SSC
        click-to-call (e.g. their Exotel user_id). Default: unsupported (None)."""
        return None

    async def resolve_agent_sip(self, email: str) -> Optional[str]:
        """A telecaller's email -> their SIP id (e.g. `sip:naveenh37746fa6`) so an
        INBOUND call can be routed to that agent's WebRTC softphone. None if the agent
        isn't provisioned — inbound then skips them (PSTN fallback only when the
        `exotel_ssc_fallback` toggle is on)."""
        return None

    async def list_caller_ids(self) -> list:
        """The caller-ID numbers (DIDs) the CRM may dial out from, each as
        `{"number": ..., "label": ...}`, scoped to the CRM's own call flow.
        Serves the SSC ExoPhone picker. Default: none."""
        return []

    async def agent_device_status(self, emails: list) -> dict:
        """Map each agent email -> `{sip, verified}`: softphone provisioning + health.
        `sip` None = no softphone (the agent cannot call — it's the only path);
        `verified` False = an unverified device is stuck active outbound (10725),
        True = healthy, None when no SIP. Display-only for the admin view. Default: none."""
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
    """Null adapter: keeps routes and tests working when no Exotel creds are
    configured (dev/test). Parses a generic payload; everything else defaults.

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
