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
    state: Optional[str] = None
    pincode: Optional[str] = None
    country: Optional[str] = None
    source: Optional[LeadSource] = None
    lead_score: Optional[int] = None
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
    outcome: CallOutcome
    note: Optional[str] = None
    follow_up_at: Optional[datetime] = None


class AssignRequest(BaseModel):
    telecaller_id: str


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


__all__ = [
    "LeadCreateRequest", "LeadUpdateRequest", "StageChangeRequest",
    "NoteRequest", "CallLogRequest", "AssignRequest",
    "LeadActivityResponse", "LeadResponse", "LeadDetailResponse", "LeadListResponse",
]
