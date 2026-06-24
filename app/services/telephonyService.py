"""Telephony service (Feature 3) — application logic over the telephony port.

Radical Minds owns outbound dialing (their iframe), inbound routing (their
campaign rules), and agent presence. So the only backend job left is: when RM's
call webhook arrives, log the call (status, duration, recording) onto the
matching lead's activity timeline, attributed to the agent who took it.

Called from a thin webhook router with a `provider` injected via
`Depends(get_telephony_provider)`.
"""
import logging
from typing import Any, Mapping, Optional

from core.telephony import TelephonyProvider, CallEvent, CallStatus, CallEventKind
from managers import UserManager
from utils.constants import UserRole
from utils.crm_enums import CallOutcome, LeadActivityType
from utils import dedup_utils
from services import leadService

logger = logging.getLogger(__name__)

# Normalized vendor call status -> the CRM's call-outcome label.
_STATUS_OUTCOME = {
    CallStatus.COMPLETED: CallOutcome.ANSWERED,
    CallStatus.NO_ANSWER: CallOutcome.NOT_ANSWERED,
    CallStatus.BUSY: CallOutcome.BUSY,
    CallStatus.FAILED: CallOutcome.NOT_ANSWERED,
    CallStatus.CANCELED: CallOutcome.NOT_ANSWERED,
}


def _status_to_outcome(status: CallStatus) -> CallOutcome:
    """Pure map from a finished call's status to the timeline outcome label."""
    return _STATUS_OUTCOME.get(status, CallOutcome.NOT_ANSWERED)


async def handle_event(engine, provider: TelephonyProvider,
                       payload: Mapping[str, Any]) -> CallEvent:
    """Process a dialer call webhook. We act on call-end (log the outcome);
    call-start is a no-op now that RM tracks agent presence."""
    event = provider.parse_event(payload)
    if event.kind == CallEventKind.ENDED:
        telecaller = await _resolve_telecaller(engine, event.telecaller_id, event.agent_number)
        await _log_call_outcome(engine, event, telecaller)
    return event


async def _log_call_outcome(engine, event: CallEvent, telecaller) -> None:
    """Auto-log a finished call (duration, outcome, recording URL) on the lead (3.4/3.5)."""
    lead = None
    for number in (event.caller, event.called):   # inbound: caller is the lead; outbound: called is
        if number:
            lead = await dedup_utils.find_duplicate(engine, mobile=number)
            if lead:
                break
    if lead is None:
        logger.info(f"[telephony] call {event.call_id} matched no lead; not logged")
        return

    outcome = _status_to_outcome(event.status)
    await leadService.record_activity(
        engine, lead.uid, LeadActivityType.CALL_LOG,
        user_id=(telecaller.uid if telecaller else None),   # None -> system-logged
        outcome=outcome.value,
        body=f"{event.direction.value.title()} call · {outcome.value}",
        details={
            "duration_seconds": event.duration_seconds,
            "recording_url": event.recording_url,   # 3.5 — played back from the timeline
            "call_id": event.call_id,
            "direction": event.direction.value,
            "agent_number": event.agent_number,
        },
    )


async def _resolve_telecaller(engine, telecaller_id: Optional[str],
                              agent_number: Optional[str]):
    """Map a webhook's agent back to a telecaller — by id if RM sends one, else by
    matching `agent_number` to a telecaller's stored phone."""
    if telecaller_id:
        try:
            return await UserManager(engine).fetch(telecaller_id)
        except Exception:
            pass
    if agent_number:
        found = await UserManager(engine).fetch_all(
            filters={"phone": agent_number, "role": UserRole.TELECALLER})
        if found.items:
            return found.items[0]
    return None


if __name__ == "__main__":
    # ponytail: smoke check for the one pure branch (DB paths need a live engine).
    assert _status_to_outcome(CallStatus.COMPLETED) == CallOutcome.ANSWERED
    assert _status_to_outcome(CallStatus.BUSY) == CallOutcome.BUSY
    assert _status_to_outcome(CallStatus.NO_ANSWER) == CallOutcome.NOT_ANSWERED
    assert _status_to_outcome(CallStatus.FAILED) == CallOutcome.NOT_ANSWERED
    print("telephony service mapping OK")
