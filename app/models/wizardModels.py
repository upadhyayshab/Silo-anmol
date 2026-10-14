from typing import List, Optional

from pydantic import BaseModel, Field


class WizardQuestionOption(BaseModel):
    label: str
    value: str


class WizardQuestion(BaseModel):
    id: str
    type: str
    label: str
    description: Optional[str] = None
    options: Optional[List[WizardQuestionOption]] = None
    required: bool = True


class FarmContext(BaseModel):
    farm_id: str
    name: str
    location: Optional[List[float]] = None
    crop: Optional[str] = None
    has_active_crop: bool = False
    irrigation: Optional[List[str]] = None
    boundary_status: Optional[str] = None


class WizardQuestionsResponse(BaseModel):
    farm_context: Optional[FarmContext] = None
    questions: List[WizardQuestion]


class WizardAnswersPayload(BaseModel):
    """Matches FM_frontend_api_contract_guide.md's WizardAnswersPayload TS
    interface field-for-field."""
    location: Optional[List[float]] = Field(None, description="[lng, lat]")
    pincode: Optional[str] = Field(None, description="6-digit Indian PIN code fallback when no GPS location")
    season: str
    has_active_crop: Optional[str] = None  # "Yes" | "No" | string, per the TS interface
    crop: Optional[str] = None
    sowing_date: Optional[str] = None
    previous_crop: Optional[str] = None
    irrigation: Optional[List[str]] = None
    budget: Optional[str] = None
    livestock: Optional[str] = None
    farm_name: Optional[str] = None


class WizardSubmitRequest(BaseModel):
    farmId: Optional[str] = None
    subscription_id: str = Field(..., description="Active subscription id, used for usage metering")
    wizard_session_id: Optional[str] = Field(None, description="Client-generated idempotency key, forwarded as-is")
    answers: WizardAnswersPayload


class WizardRecommendationItem(BaseModel):
    id: str
    crop_name: str
    suitability_score: float
    roi_estimate: str
    duration_days: Optional[int] = None
    water_requirement: Optional[str] = None
    reason: str


class WizardSubmitResponse(BaseModel):
    recommendations: List[WizardRecommendationItem]


__all__ = [
    "WizardQuestionOption", "WizardQuestion", "FarmContext", "WizardQuestionsResponse",
    "WizardAnswersPayload", "WizardSubmitRequest", "WizardRecommendationItem", "WizardSubmitResponse",
]
