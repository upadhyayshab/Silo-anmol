from datetime import datetime
from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, Field

ScanType = Literal["all", "disease_diagnosis", "cow_dung"]


class UnifiedDiseaseReportItem(BaseModel):
    report_id: str
    scan_type: Literal["disease_diagnosis"] = "disease_diagnosis"
    user_id: str
    cow_id: Optional[str] = None
    lang_code: str = "en"
    img_url: Optional[str] = None
    created_at: datetime
    title: Optional[str] = None
    primary_diagnosis: Optional[str] = None
    detected_count: int = 0
    suspected_count: int = 0
    call_vet: bool = False
    is_cattle_image: bool = True
    image_view: Optional[str] = None
    findings: Optional[list[Any]] = None
    reasoning: Optional[str] = None
    model: Optional[str] = None
    pr: Optional[dict[str, Any]] = None


class UnifiedManureReportItem(BaseModel):
    report_id: str
    scan_type: Literal["cow_dung"] = "cow_dung"
    user_id: str
    cow_id: Optional[str] = None
    lang_code: str = "en"
    img_url: Optional[str] = None
    created_at: datetime
    title: str
    manure_score: int
    manure_sub_score: Optional[str] = None
    severity: Optional[str] = None
    priority: Optional[str] = None
    call_vet: bool = False
    vet_gate: Optional[str] = None
    score_breakdown: Optional[dict[str, str]] = None
    visual_evidence_as_tags: Optional[list[Any]] = None
    possible_conditions: Optional[list[str]] = None
    recommended_actions: Optional[list[str]] = None
    call_vet_if: Optional[str] = None
    re_scan_window: Optional[str] = None
    milk_withdrawal_risk: Optional[str] = None
    premium: Optional[dict[str, Any]] = None
    pr: Optional[dict[str, Any]] = None
    reasoning: Optional[str] = None
    model: Optional[str] = None


UnifiedReportItem = Union[UnifiedDiseaseReportItem, UnifiedManureReportItem]


class ReportsListResponse(BaseModel):
    total: int = Field(..., description="Total count of reports matching the filter criteria.")
    limit: int = Field(..., description="Pagination limit.")
    offset: int = Field(..., description="Pagination offset.")
    items: list[UnifiedReportItem] = Field(..., description="List of report items sorted newest first.")


__all__ = [
    "ScanType",
    "UnifiedDiseaseReportItem",
    "UnifiedManureReportItem",
    "UnifiedReportItem",
    "ReportsListResponse",
]
