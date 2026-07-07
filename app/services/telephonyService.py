"""Telephony service (Feature 3) — application logic over the telephony port.

The in-browser WebRTC softphone is the only call path. This service owns what the
softphone can't do itself: logging finished calls onto the matching lead's timeline
(webhook + CDR enrichment) and routing inbound calls to an available agent's SIP
softphone (owner-first, else round-robin over presence, else Exotel's queue).

Called from a thin webhook router with a `provider` injected via
`Depends(get_telephony_provider)`.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from core.telephony import TelephonyProvider, CallEvent, CallStatus, CallEventKind, CallDirection
from managers import UserManager, LeadManager
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
    call-start is a no-op (agent presence comes from the frontend heartbeat)."""
    event = provider.parse_event(payload)
    if event.kind == CallEventKind.ENDED:
        telecaller = await _resolve_telecaller(engine, provider, event)
        await _log_call_outcome(engine, event, telecaller)
    return event


async def _log_call_outcome(engine, event: CallEvent, telecaller) -> None:
    """Auto-log a finished call (duration, outcome, recording URL) on the lead (3.4/3.5)."""
    # Outbound click-to-call echoes the lead uid back in `reference` (custom_field),
    # so we log straight to it — no fragile number-matching. Inbound (Passthru) has no
    # reference, so it falls back to matching the lead by phone number.
    lead_id = None
    if event.reference and event.reference.startswith("leads_"):
        lead_id = event.reference
    else:
        for number in (event.caller, event.called):   # inbound: caller is the lead; outbound: called is
            if number:
                lead = await dedup_utils.find_duplicate(engine, mobile=number)
                if lead:
                    lead_id = lead.uid
                    break

    # Inbound from an unknown number -> capture the caller as a new lead (answered OR
    # missed, so no prospect slips through). Owner = the agent who took it (create_lead
    # attributes a lead to its creator); a missed call has no agent, so passing "system"
    # makes create_lead round-robin an owner to follow up. Dedup is handled inside.
    if lead_id is None and event.direction == CallDirection.INBOUND and event.caller:
        from models import LeadCreateRequest
        from utils.crm_constants import LeadSource
        new_lead, _ = await leadService.create_lead(
            engine,
            LeadCreateRequest(first_name="Inbound Caller", mobile=event.caller,
                              source=LeadSource.INBOUND_PHONE_CALL),
            by_user_id=(telecaller.uid if telecaller else "system"),
            source_label="Inbound call",
        )
        lead_id = new_lead.uid

    if lead_id is None:
        logger.info(f"[telephony] call {event.call_id} matched no lead; not logged")
        return

    # Inbound call handled by a non-owner -> grant them rights to act on this lead
    # (disposition/orders/activities), owner unchanged. Mirrors the route-time grant in
    # resolve_inbound_agent, and also covers setups where inbound isn't routed via our
    # /exotel/inbound endpoint. Idempotent.
    if event.direction == CallDirection.INBOUND and telecaller:
        try:
            lead_row = await LeadManager(engine).fetch(lead_id)
            if getattr(lead_row, "owner_id", None) != telecaller.uid:
                from services import assignmentService
                await assignmentService.grant_call_access(engine, lead_id, telecaller.uid)
        except Exception as e:
            logger.warning(f"[telephony] could not grant call-access on {lead_id}: {e}")

    outcome = _status_to_outcome(event.status)
    await leadService.record_activity(
        engine, lead_id, LeadActivityType.CALL_LOG,
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


def _is_connected(status: Optional[CallStatus]) -> bool:
    """Did the customer pick up? The CDR marks an answered call 'completed'; no-answer /
    busy / failed mean it never connected. Keyed off STATUS, not talk time — the CDR's
    Duration field lags its Status (often still 0 right after hang-up), which would
    mis-read a real conversation as 'not connected'. Pre-fill DEFAULT, agent overrides."""
    return status == CallStatus.COMPLETED


async def probe_call_connected(provider, call_sid: str) -> Optional[bool]:
    """Poll the CDR briefly after hang-up and report whether the call connected, so the
    disposition picker can pre-fill Connected / Not Connected. Returns None if the CDR
    never settles in the wait window (picker then stays blank — no wrong default)."""
    if not call_sid:
        return None
    for delay in (3, 5, 8):   # match the recording-enrich window so the status is final
        await asyncio.sleep(delay)
        details = await provider.fetch_call_details(call_sid)
        status = details.get("status") if details else None
        if status and status not in (CallStatus.IN_PROGRESS, CallStatus.RINGING):
            result = _is_connected(status)
            logger.info(f"[telephony] probe {call_sid}: raw_status={details.get('raw_status')!r} "
                        f"dur={details.get('duration_seconds')} -> connected={result}")
            return result
    logger.info(f"[telephony] probe {call_sid}: CDR never settled -> no pre-fill")
    return None


async def enrich_disposition_with_cdr(engine, provider, *, activity_uid: str,
                                      call_sid: str, direction: str = "outbound") -> None:
    """Fold the softphone CDR (recording URL + real duration) into the disposition the
    agent just logged, so a softphone call is ONE timeline row, not two. The WebRTC SDK
    reports no outcome and fires no webhook, and the CDR settles a few seconds after
    hang-up, so we poll briefly then patch the existing CALL_LOG activity. The agent's
    picked outcome wins; we only add the recording/duration the CDR uniquely knows."""
    details = None
    for delay in (3, 5, 8):   # let the CDR (and recording) settle after hang-up
        await asyncio.sleep(delay)
        details = await provider.fetch_call_details(call_sid)
        status = details.get("status") if details else None
        if status and status not in (CallStatus.IN_PROGRESS, CallStatus.RINGING):
            break

    if not (details and details.get("status")):
        logger.info(f"[telephony] disposition CDR {call_sid}: never settled; no recording attached")
        return

    await leadService.attach_call_details(engine, activity_uid, {
        "duration_seconds": details.get("duration_seconds"),
        "recording_url": details.get("recording_url"),
        "call_id": call_sid,
        "direction": direction,
        "via": "softphone",
    })


async def _resolve_telecaller(engine, provider, event: CallEvent):
    """Map a webhook's agent back to a telecaller, in order:
      1. an explicit telecaller id, if the vendor sent one;
      2. the agent's phone (outbound legs carry a real number);
      3. the agent's email — the adapter resolves a vendor SIP id to an email (e.g.
         Exotel `sip:naveenh…` -> the agent's Exotel email), which matches the
         telecaller's CRM email (both are the same corporate address).
    """
    if event.telecaller_id:
        try:
            return await UserManager(engine).fetch(event.telecaller_id)
        except Exception:
            pass
    if event.agent_number:
        found = await UserManager(engine).fetch_all(
            filters={"phone": event.agent_number, "role": UserRole.TELECALLER})
        if found.items:
            return found.items[0]
        email = await provider.resolve_agent_email(event.agent_number)
        if email:
            by_email = await UserManager(engine).fetch_all(filters={"email": email})
            if by_email.items:
                return by_email.items[0]
    return None


# ---------------------------------------------------------------------------
# Inbound routing (Feature 3.2) — "who do we ring when a call comes in?"
# ---------------------------------------------------------------------------

@dataclass
class InboundRoute:
    """The routing answer for an inbound call. `dial_number` is the agent's SIP id,
    or None (no agent / no softphone) — the Exotel applet then queues the call."""
    telecaller_id: Optional[str]
    dial_number: Optional[str]
    reason: str                       # "owner" | "round_robin" | "no_agent"
    lead_id: Optional[str] = None


def _decide_inbound(owner_id: Optional[str], owner_available: bool,
                    pick_id: Optional[str]) -> Tuple[Optional[str], str]:
    """Pure routing decision: ring the lead's owner if they're available, else the
    round-robin pick among available agents, else no agent. Kept side-effect-free so
    the policy is unit-testable without a DB."""
    if owner_id and owner_available:
        return owner_id, "owner"
    if pick_id:
        return pick_id, "round_robin"
    return None, "no_agent"


async def resolve_inbound_agent(engine, caller_number: str, provider=None) -> InboundRoute:
    """Decide who an inbound call should ring. Owner-first (if the caller matches a
    lead whose owner is available), else the least-loaded available telecaller scoped
    to the lead's outlet/state, else no agent (NO_AGENT -> Exotel queue applet).

    `dial_number` is the chosen agent's **SIP id** (rings their in-browser softphone)
    or None — there is no PSTN fallback; an agent without a softphone mapping is
    treated like no agent and Exotel's queue takes the call. `provider=None` (tests)
    therefore always yields dial_number=None.

    ponytail: queue/retry on NO_AGENT is delegated to Exotel's Queue applet for now;
    this function is the seam where a backend call-queue would hook in later.
    """
    from services import assignmentService, presenceService   # lazy: avoid import cycle

    lead = await dedup_utils.find_duplicate(engine, mobile=caller_number)
    avail = await presenceService.available_ids(engine)

    owner_id = getattr(lead, "owner_id", None) if lead else None
    owner_available = bool(owner_id and owner_id in avail)

    pick_id = None
    if not owner_available:
        outlet_id = getattr(lead, "outlet_id", None) if lead else None
        state = getattr(lead, "state", None) if lead else None
        pick = await assignmentService.pick_telecaller(
            engine, outlet_id, state=state, only_ids=avail)
        pick_id = pick.uid if pick else None

    chosen_id, reason = _decide_inbound(owner_id, owner_available, pick_id)

    # Routed to someone who isn't the owner -> grant them rights to act on the lead
    # (disposition/orders/activities) post-call, without changing ownership. Owner-routed
    # and no-agent calls need no grant.
    if chosen_id and lead and chosen_id != owner_id:
        await assignmentService.grant_call_access(engine, lead.uid, chosen_id)
        logger.info(f"[telephony] granted call-access lead={lead.uid} -> agent={chosen_id} (owner={owner_id})")

    dial_number = None
    if chosen_id:
        agent = await UserManager(engine).fetch(chosen_id)
        # SIP-only: ring the in-browser softphone. No PSTN fallback — if the agent has
        # no softphone mapping we return None and the empty destination sends the call
        # to Exotel's queue, same as no_agent.
        if provider is not None and getattr(agent, "email", None):
            try:
                dial_number = await provider.resolve_agent_sip(agent.email)
            except Exception:
                dial_number = None

    return InboundRoute(
        telecaller_id=chosen_id, dial_number=dial_number, reason=reason,
        lead_id=(lead.uid if lead else None),
    )


if __name__ == "__main__":
    # ponytail: smoke check for the pure branches (DB paths need a live engine).
    assert _status_to_outcome(CallStatus.COMPLETED) == CallOutcome.ANSWERED
    assert _status_to_outcome(CallStatus.BUSY) == CallOutcome.BUSY
    assert _status_to_outcome(CallStatus.NO_ANSWER) == CallOutcome.NOT_ANSWERED
    assert _status_to_outcome(CallStatus.FAILED) == CallOutcome.NOT_ANSWERED

    # connected? (disposition pre-fill heuristic — keyed off status, not duration)
    assert _is_connected(CallStatus.COMPLETED) is True
    assert _is_connected(CallStatus.NO_ANSWER) is False
    assert _is_connected(CallStatus.BUSY) is False
    assert _is_connected(CallStatus.FAILED) is False
    assert _is_connected(None) is False

    # inbound routing decision
    assert _decide_inbound("u1", True, "u2") == ("u1", "owner")        # owner online -> owner
    assert _decide_inbound("u1", False, "u2") == ("u2", "round_robin") # owner offline -> pick
    assert _decide_inbound(None, False, "u2") == ("u2", "round_robin") # no owner -> pick
    assert _decide_inbound("u1", False, None) == (None, "no_agent")    # owner offline, none free
    assert _decide_inbound(None, False, None) == (None, "no_agent")    # nobody at all
    print("telephony service mapping + inbound routing OK")
