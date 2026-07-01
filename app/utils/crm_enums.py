"""CRM (native lead module) enums — Stage 1 Data Foundation.

Kept separate from `crm_constants.py` (which holds the legacy LeadSquared
field mappings) so the new native-CRM code has a clean namespace.
"""
from enum import Enum


class LeadStage(str, Enum):
    """Lead pipeline stages.

    Fixed for Stage 1. Becomes a configurable `lead_stages` table later
    (Feature 8 — rule engine). Values are the human-readable labels.
    """
    NEW_LEAD = "New Lead"
    ENGAGED = "Engaged"
    LAPSED = "Lapsed"
    RTU = "RTU"            # Ready To Use (warm lead)
    FTU = "FTU"            # First Time User (placed first order)
    NOT_QUALIFIED = "Not Qualified"
    NOT_REACHABLE = "Not Reachable"


class LeadActivityType(str, Enum):
    """Types of entries that appear in a lead's activity timeline."""
    CREATED = "created"
    ASSIGNMENT = "assignment"
    FIELD_UPDATE = "field_update"
    STAGE_CHANGE = "stage_change"
    NOTE = "note"
    CALL_LOG = "call_log"
    ORDER = "order"  # an order was placed from the CRM for this lead
    ORDER_UPDATE = "order_update" # an order's status/payment/delivery was updated
    RE_ENGAGED = "re_engaged"  # a dormant lead re-submitted a form and was reopened/reassigned


class CallOutcome(str, Enum):
    """Outcome of a manually logged call (Stage 2 workflow)."""
    ANSWERED = "answered"
    NOT_ANSWERED = "not_answered"
    BUSY = "busy"
    WRONG_NUMBER = "wrong_number"
    SWITCHED_OFF = "switched_off"
    CALL_BACK_LATER = "call_back_later"   # answered but asked to be called again


# Two-level call disposition the telecaller picks after every call (frontend taxonomy in
# leadEnums.js DISPOSITIONS). Each sub-disposition maps to a CallOutcome; unmapped subs
# fall back to "answered". `DNC_SUB_DISPOSITIONS` also flip the lead's do_not_call flag.
DISPOSITION_OUTCOME = {
    "Ringing No Response": CallOutcome.NOT_ANSWERED.value,
    "Busy": CallOutcome.BUSY.value,
    "Switched off": CallOutcome.SWITCHED_OFF.value,
    "Not Reachable/Out of Coverage": CallOutcome.NOT_ANSWERED.value,
    "Call dropped": CallOutcome.NOT_ANSWERED.value,
    "Call Back": CallOutcome.CALL_BACK_LATER.value,
    "Order Booked": CallOutcome.ANSWERED.value,
    "Interested": CallOutcome.ANSWERED.value,
    "Not Interested": CallOutcome.ANSWERED.value,
    "Do Not Call": CallOutcome.ANSWERED.value,
    "Wrong Number": CallOutcome.WRONG_NUMBER.value,
    "Invalid Number": CallOutcome.WRONG_NUMBER.value,
}
DNC_SUB_DISPOSITIONS = {"Do Not Call"}

# Sub-disposition -> stage auto-applied after a call is logged. "Order Booked" is intentionally
# absent: its conversion stage (FTU/RTU) comes from real order placement (handle_post_order), not
# the disposition alone. A lead in PROTECTED_STAGES is never auto-demoted by a later call.
DISPOSITION_STAGE = {
    # Not Connected
    "Ringing No Response": LeadStage.NOT_REACHABLE,
    "Busy": LeadStage.NOT_REACHABLE,
    "Switched off": LeadStage.NOT_REACHABLE,
    "Not Reachable/Out of Coverage": LeadStage.NOT_REACHABLE,
    "Call dropped": LeadStage.NOT_REACHABLE,
    "Invalid Number": LeadStage.NOT_QUALIFIED,
    # Connected
    "Call Back": LeadStage.ENGAGED,
    "Interested": LeadStage.ENGAGED,
    "Not Interested": LeadStage.NOT_QUALIFIED,
    "Do Not Call": LeadStage.NOT_QUALIFIED,
    "Wrong Number": LeadStage.NOT_QUALIFIED,
}
# Converted stages we never auto-demote on a later disposition (value strings for easy compare).
PROTECTED_STAGES = {LeadStage.FTU.value, LeadStage.RTU.value}


class AssignmentReason(str, Enum):
    """Why a lead was assigned to a telecaller."""
    ROUND_ROBIN = "round_robin"   # auto-distributed (system/webhook leads, or admin bulk distribute)
    MANUAL = "manual"             # explicitly (re)assigned to a chosen telecaller
    SELF_CREATED = "self_created" # attributed to the user who created the lead
    INBOUND_CALL_ACCESS = "inbound_call_access"  # handled a routed inbound call; may act, NOT the owner
