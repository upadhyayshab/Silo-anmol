from typing import Optional

from pydantic import BaseModel, Field


class ManureScoreResult(BaseModel):
    """Minimal response for /detect/manure while the full farmer-facing body
    per score is still pending. health_status/priority are deterministic
    lookups, not model output — only manure_score comes from Gemini."""

    manure_score: int = Field(..., ge=1, le=5, description="The dung score (1-5) predicted from the image.")
    health_status: str = Field(..., description="Health meaning for this score (e.g. 'Healthy').")
    priority: str = Field(..., description="Priority level: Emergency, Moderate, Normal, or High.")
    reasoning: str = Field(..., description="One-sentence model reasoning for the predicted score.")
    image_url: Optional[str] = Field(
        None, description="The image that was scored — the given image_url, or 'upload:<filename>' for a file upload."
    )


class ManureSubcategoryResult(BaseModel):
    """Response for /detect/manure/subcategory — the sub-code (colour/condition)
    classification. Every field except reasoning is a deterministic lookup on
    the matched row of the master table, not model output."""

    score: int = Field(..., ge=1, le=5, description="Consistency score (1-5) the sub-code belongs to.")
    sub_code: str = Field(..., description="The matched sub-code, e.g. '1-GRN'.")
    sub_type_name: str = Field(..., description="Human-readable name of the sub-code.")
    colour_group: str = Field(..., description="Colour group code, e.g. 'GRN', 'YEL', 'BLK'.")
    diagnosed_clinical_condition: str = Field(..., description="The clinical condition this sub-code indicates.")
    severity: str = Field(..., description="Severity tier: NORMAL_BASELINE, MONITOR, WARNING, URGENT, or EMERGENCY.")
    vet_gate: str = Field(..., description="Whether a vet visit is required: NO, ADVISED, or MANDATORY.")
    message: str = Field(..., description="Farmer-facing message, composed by the shared message builder.")
    reasoning: str = Field(..., description="One-to-two sentence model reasoning for the matched sub-code.")
    image_url: Optional[str] = Field(
        None, description="The image that was classified — the given image_url, or 'upload:<filename>' for a file upload."
    )


__all__ = ["ManureScoreResult", "ManureSubcategoryResult"]
