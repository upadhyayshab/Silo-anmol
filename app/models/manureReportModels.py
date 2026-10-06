from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class VisualEvidenceTag(BaseModel):
    tag: str = Field(..., description="Evidence category the app maps to an icon, e.g. 'Consistency', 'Form'.")
    value: str = Field(..., description="Short farmer-facing description of what was seen.")


class ManurePremiumBlock(BaseModel):
    """Sub-code detail, returned only when has_subs is true."""

    manure_sub_score: str = Field(..., description="Matched manure_kb sub-code, e.g. '4-S-W'.")
    sub_type_name: Optional[str] = None
    root_cause_etiology: Optional[str] = None
    on_farm_confirmatory_check: Optional[str] = None
    reasoning: str = Field(..., description="Model's reasoning for the matched sub-code.")


class ManureReportResponse(BaseModel):
    """Response for POST /manure/report. Gemini supplies only the sub-code,
    visual_evidence_as_tags and reasoning; the rest is built from manure_kb
    (kb_reference_id = the kb_uid of the entry used) and fixed score rules.
    cow_id is omitted entirely when the request did not send one."""

    report_id: str
    user_id: str
    cow_id: Optional[str] = Field(None, description="Present only when sent in the request.")
    subs_id: Optional[str] = None
    has_subs: bool
    ui_version: str = Field(..., description="'1.1' when has_subs, else '1.0'.")
    lang_code: str
    kb_reference_id: str
    created_at: datetime
    img_url: str

    manure_score: int = Field(..., ge=1, le=5)
    title: str
    severity: Optional[str] = None
    priority: str
    call_vet: bool = Field(..., description="True for scores 1 and 5.")
    vet_gate: Optional[str] = None
    score_breakdown: dict[str, str]

    possible_conditions: list[str]
    visual_evidence_as_tags: list[VisualEvidenceTag]
    recommended_actions: list[str]
    call_vet_if: str
    re_scan_window: Optional[str] = None
    milk_withdrawal_risk: Optional[str] = None

    premium: Optional[ManurePremiumBlock] = None
    pr: dict[str, Any] = Field(default_factory=dict, description="Product recommendations (to be filled from the KB).")


__all__ = ["VisualEvidenceTag", "ManurePremiumBlock", "ManureReportResponse"]
