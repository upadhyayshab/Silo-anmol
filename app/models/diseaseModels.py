from typing import Literal, Optional

from pydantic import BaseModel, Field

Confidence = Literal["LOW", "MEDIUM", "HIGH"]
Severity = Literal["Healthy", "Mild", "Moderate", "Severe", "Critical"]
FindingStatus = Literal["detected", "suspected", "not_detected", "not_assessable"]


class DiseaseFinding(BaseModel):
    """One disease's verdict for the image. status/confidence/severity/signs/
    reasoning come from the model (with server-side rules applied); every other
    field is a deterministic lookup on the disease's KB row."""

    disease_id: str = Field(..., description="KB id, e.g. 'D01'.")
    disease_name: str = Field(..., description="KB disease name, e.g. 'Bloat'.")
    status: FindingStatus = Field(
        ...,
        description=(
            "'detected' (MEDIUM/HIGH confidence), 'suspected' (LOW confidence), "
            "'not_detected', or 'not_assessable' (required view/region not visible)."
        ),
    )
    confidence: Optional[Confidence] = Field(None, description="Per the KB confidence rules; null unless detected/suspected.")
    severity: Severity = Field(..., description="Severity grade; 'Healthy' unless detected/suspected.")
    signs_observed: list[str] = Field(default_factory=list, description="Signs of this disease seen in the image.")
    signs_against: list[str] = Field(default_factory=list, description="Signs seen that argue against this disease.")
    reasoning: str = Field(..., description="One-to-two sentence model reasoning.")
    severity_and_urgency: str = Field(..., description="KB urgency guidance for this disease.")
    confirmatory_action: str = Field(..., description="KB confirmatory action (what the vet should do).")
    farmer_questions: list[str] = Field(default_factory=list, description="KB follow-up questions to ask the farmer.")
    retake_hint: Optional[str] = Field(
        None, description="KB image requirements for this disease, set only when status is 'not_assessable'."
    )


class DiseaseDetectionResult(BaseModel):
    """Response for /detect/disease."""

    is_cattle_image: bool = Field(..., description="Whether the image is a real photo of a cow or buffalo.")
    image_view: str = Field(..., description="The view the photo shows, e.g. 'left side', 'face front'.")
    findings: list[DiseaseFinding] = Field(..., description="One finding per screened disease, in KB order.")
    model: str = Field(..., description="Gemini model that produced the assessment.")
    image_url: Optional[str] = Field(
        None, description="The image that was screened — the given image_url, or 'upload:<filename>' for a file upload."
    )


__all__ = ["DiseaseFinding", "DiseaseDetectionResult"]
