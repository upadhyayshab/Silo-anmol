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
    "Interested Call Back": CallOutcome.CALL_BACK_LATER.value,
    "Not Interested": CallOutcome.ANSWERED.value,
    "Do Not Call": CallOutcome.ANSWERED.value,
    "Invalid Number": CallOutcome.WRONG_NUMBER.value,
}
DNC_SUB_DISPOSITIONS = {"Do Not Call"}


class AssignmentReason(str, Enum):
    """Why a lead was assigned to a telecaller."""
    ROUND_ROBIN = "round_robin"   # auto-distributed (system/webhook leads, or admin bulk distribute)
    MANUAL = "manual"             # explicitly (re)assigned to a chosen telecaller
    SELF_CREATED = "self_created" # attributed to the user who created the lead
    INBOUND_CALL_ACCESS = "inbound_call_access"  # handled a routed inbound call; may act, NOT the owner
