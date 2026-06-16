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


class CallOutcome(str, Enum):
    """Outcome of a manually logged call (Stage 2 workflow, endpoint ready now)."""
    ANSWERED = "answered"
    NOT_ANSWERED = "not_answered"
    BUSY = "busy"
    WRONG_NUMBER = "wrong_number"
    SWITCHED_OFF = "switched_off"


class AssignmentReason(str, Enum):
    """Why a lead was assigned to a telecaller."""
    ROUND_ROBIN = "round_robin"
    MANUAL = "manual"
