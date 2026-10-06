from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from models.diseaseModels import DiseaseFinding


class DiseaseReportResponse(BaseModel):
    """Response for POST /disease/report."""

    report_id: str
    user_id: str
    cow_id: Optional[str] = Field(None, description="Present only when sent in the request.")
    subs_id: Optional[str] = None
    has_subs: bool = True
    ui_version: str = Field("1.0", description="UI layout version.")
    lang_code: str = "en"
    created_at: datetime
    img_url: str

    is_cattle_image: bool = Field(..., description="Whether the image is a real photo of a cow or buffalo.")
    image_view: str = Field(..., description="The view shown in the photo, e.g. 'left side', 'face front'.")

    primary_diagnosis: Optional[str] = Field(None, description="Top detected disease name or id.")
    detected_count: int = Field(0, description="Number of detected diseases.")
    suspected_count: int = Field(0, description="Number of suspected diseases.")
    call_vet: bool = Field(False, description="True if any finding requires urgent/immediate vet attention.")

    findings: list[DiseaseFinding] = Field(..., description="One finding per screened disease.")
    reasoning: Optional[str] = Field(None, description="Summary model reasoning for the overall screening.")
    model: str = Field(..., description="Gemini model used.")
    pr: dict[str, Any] = Field(default_factory=dict, description="Product recommendations or follow-up items.")


__all__ = ["DiseaseReportResponse"]
