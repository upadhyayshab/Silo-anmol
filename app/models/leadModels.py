"""Request/response DTOs for the native CRM Lead module (Stage 1).

Re-exported from `models/__init__.py`.
"""
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, ConfigDict

from utils.crm_enums import LeadStage, CallOutcome
from utils.crm_constants import LeadSource


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------

class LeadCreateRequest(BaseModel):
    first_name: str
    last_name: Optional[str] = None
    mobile: str
    phone: Optional[str] = None
    email: Optional[str] = None
    address_line: Optional[str] = None
    address_line_2: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    taluk: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    country: Optional[str] = None
    source: Optional[LeadSource] = None
    lead_score: Optional[int] = None
    # Contact-preference flags (honoured on import from LSQ opt-outs).
    do_not_call: Optional[bool] = None
    do_not_sms: Optional[bool] = None
    do_not_email: Optional[bool] = None
    custom_fields: Optional[Dict[str, Any]] = None
    campaign_data: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    # Admins may force a specific owner; otherwise round-robin assigns one.
    owner_id: Optional[str] = None


class LeadUpdateRequest(BaseModel):
    """All-optional patch. Stage changes go through /stage, owner through /assign."""
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    mobile: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address_line: Optional[str] = None
    address_line_2: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    taluk: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    country: Optional[str] = None
    source: Optional[LeadSource] = None
    lead_score: Optional[int] = None
    follow_up_at: Optional[datetime] = None
    do_not_call: Optional[bool] = None
    do_not_sms: Optional[bool] = None
    do_not_email: Optional[bool] = None
    custom_fields: Optional[Dict[str, Any]] = None
    campaign_data: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None


class StageChangeRequest(BaseModel):
    stage: LeadStage
    note: Optional[str] = None


class NoteRequest(BaseModel):
    body: str


class CallLogRequest(BaseModel):
    # Either pick a flat `outcome` (legacy) OR a 2-level disposition (`disposition` +
    # `sub_disposition`); the sub-disposition maps to the outcome + side effects server-side.
    outcome: Optional[CallOutcome] = None
    disposition: Optional[str] = None       # main, e.g. "Connected" / "Not Connected"
    sub_disposition: Optional[str] = None   # sub, e.g. "Order Booked" — drives outcome + effects
    note: Optional[str] = None
    follow_up_at: Optional[datetime] = None
    # Optional call length (seconds). Stored in the activity's `details` JSON.
    duration_seconds: Optional[int] = None
    # Softphone calls: the Exotel CallSid. When present, the backend pulls the CDR
    # (recording URL + real duration) and folds it into THIS disposition entry, so a
    # softphone call produces one timeline row instead of two.
    call_sid: Optional[str] = None
    direction: str = "outbound"


class AssignRequest(BaseModel):
    telecaller_id: str


class DistributeRequest(BaseModel):
    """Admin bulk round-robin distribution of leads across telecallers."""
    lead_ids: List[str]
    # Target pool; if omitted, all active telecallers are used.
    telecaller_ids: Optional[List[str]] = None


class DistributeResponse(BaseModel):
    assigned: int
    skipped: int
    by_telecaller: Dict[str, int]
    detail: Optional[str] = None


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------

class LeadActivityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    uid: str
    lead_id: str
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    activity_type: str
    body: Optional[str] = None
    outcome: Optional[str] = None
    from_stage: Optional[str] = None
    to_stage: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None


class LeadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    uid: str
    lead_number: str
    first_name: str
    last_name: Optional[str] = None
    mobile: str
    phone: Optional[str] = None
    email: Optional[str] = None
    address_line: Optional[str] = None
    address_line_2: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    taluk: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    country: Optional[str] = None
    stage: LeadStage
    source: Optional[LeadSource] = None
    owner_id: Optional[str] = None
    owner_name: Optional[str] = None
    outlet_id: Optional[str] = None
    outlet_name: Optional[str] = None
    lead_score: Optional[int] = None
    follow_up_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    # Latest call disposition (from the most recent CALL_LOG); enriched at list-build time.
    disposition: Optional[str] = None
    sub_disposition: Optional[str] = None
    order_count: int = 0
    order_value: Decimal = Decimal("0")
    do_not_call: bool = False
    do_not_sms: bool = False
    do_not_email: bool = False
    custom_fields: Optional[Dict[str, Any]] = None
    campaign_data: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class LeadDetailResponse(LeadResponse):
    activities: List[LeadActivityResponse] = []


class LeadListResponse(BaseModel):
    items: List[LeadResponse]
    count: int   # number of items in this page
    total: int   # total rows matching the filters (for pagination)
    limit: int
    offset: int


# --------------------------------------------------------------------------
# Stage 2 — CSV import (2.4)
# --------------------------------------------------------------------------

class LeadImportError(BaseModel):
    row: int        # 1-based source row (the header is row 1)
    reason: str


class LeadImportSummary(BaseModel):
    total_rows: int
    created: int            # new leads inserted
    merged: int             # duplicates merged into existing leads
    skipped: int            # rows rejected (see `errors`)
    errors: List[LeadImportError] = []


# --------------------------------------------------------------------------
# Stage 2 — Today's callback queue (2.5)
# --------------------------------------------------------------------------

class TodayQueueBucket(BaseModel):
    count: int                       # true total in this bucket (may exceed len(items))
    items: List[LeadResponse] = []


class TodayQueueResponse(BaseModel):
    new: TodayQueueBucket            # stage New Lead
    engaged: TodayQueueBucket        # stage Engaged
    not_reachable: TodayQueueBucket  # stage Not Reachable
    generated_at: datetime


__all__ = [
    "LeadCreateRequest", "LeadUpdateRequest", "StageChangeRequest",
    "NoteRequest", "CallLogRequest", "AssignRequest",
    "DistributeRequest", "DistributeResponse",
    "LeadActivityResponse", "LeadResponse", "LeadDetailResponse", "LeadListResponse",
    "LeadImportError", "LeadImportSummary",
    "TodayQueueBucket", "TodayQueueResponse",
]
