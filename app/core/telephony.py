"""Telephony port — the vendor-agnostic interface the CRM core depends on.

Radical Minds owns the parts that used to live here:
  - Outbound click-to-call  -> their embeddable iframe (frontend, dialed in-browser)
  - Inbound call routing     -> their native campaign/ACD rules (no per-call API to us)
  - Agent availability       -> their softphone presence

That leaves the backend a single telephony job: **ingest RM's call webhook and
normalize it**, so the CRM can log the call + recording onto the lead's timeline.
The port is therefore one method — `parse_event` — kept as an ABC so a future
vendor's webhook format swaps in without touching the CRM (the Ports & Adapters
guarantee the team asked for).

The concrete `RadicalMindsAdapter(TelephonyProvider)` is wired by
`dependencies/telephony_dep.py`; `services/telephonyService.handle_event` calls
`parse_event` and writes the timeline.
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
    duration_seconds: Optional[int] = None
    recording_url: Optional[str] = None
    occurred_at: Optional[datetime] = None
    provider_raw: Optional[dict] = None


class TelephonyProvider(ABC):
    """Port: the only telephony contract the CRM core knows about."""

    @abstractmethod
    def parse_event(self, payload: Mapping[str, Any]) -> CallEvent:
        """Normalize a vendor call webhook (start / end / post-call) into a CallEvent."""


class MockTelephonyProvider(TelephonyProvider):
    """In-memory stand-in so the webhook route can be built and tested before the
    real `RadicalMindsAdapter` lands. Field guesses below are just so the mock
    accepts a generic payload in tests — the RM adapter maps RM's real fields.

    ponytail: 'null adapter' to unblock dev, not a vendor.
    """

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
    "CallEvent",
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
