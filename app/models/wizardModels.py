from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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
    search_endpoint: Optional[str] = None
    # Full crop list with names and images (Farm Monitoring's picker list,
    # shared by both features); {userId} is the farmer's id.
    options_endpoint: Optional[str] = None
    depends_on: Optional[dict] = None


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

    @field_validator("location")
    @classmethod
    def _check_location(cls, v: Optional[List[float]]) -> Optional[List[float]]:
        """Exactly [lng, lat]. A one-number pin used to crash the submit (500),
        and a swapped [lat, lng] pair was accepted and forwarded to Agro as the
        farm centre. Same inverted-pair rule as Agro's farm validator: any
        swapped Indian pair puts "latitude" above 60."""
        if v is None:
            return None
        if len(v) != 2:
            raise ValueError("location must be [longitude, latitude]")
        lon, lat = v
        if not (-180.0 <= lon <= 180.0) or abs(lat) > 60.0:
            raise ValueError(
                f"location latitude {lat} is out of range (possible inverted [lat, lon]); "
                "location must be [longitude, latitude]"
            )
        return v

    @model_validator(mode="after")
    def _check_active_crop(self):
        """Same rule as Agro's POST /farms: a farm that is growing a crop must
        say which crop and when it was sown."""
        if (self.has_active_crop or "").strip().lower() == "yes" and (not self.crop or not self.sowing_date):
            raise ValueError("has_active_crop=Yes requires crop and sowing_date")
        return self


class WizardSubmitRequest(BaseModel):
    farmId: Optional[str] = None
    subscription_id: str = Field(..., description="Active subscription id, used for usage metering")
    wizard_session_id: Optional[str] = Field(None, description="Client-generated idempotency key, forwarded as-is")
    answers: WizardAnswersPayload


class WizardRecommendationItem(BaseModel):
    id: str
    crop_id: Optional[str] = None  # crop_reports.crop_id: use it for /recommendations/{id}/reports/{crop_id}
    crop_name: str
    crop_name_vernacular: Optional[str] = None
    suitability_score: float
    is_top_pick: bool = False
    roi_estimate: str
    duration_days: Optional[int] = None
    years_to_first_harvest: Optional[float] = None  # perennials, which have no duration_days
    water_requirement: Optional[str] = None
    reason: str
    match_reasons: List[str] = Field(default_factory=list)


class WizardContext(BaseModel):
    zone_filter_applied: bool = False
    soil_texture_label: Optional[str] = None
    notes: List[str] = Field(default_factory=list)


class SeasonSuggestion(BaseModel):
    """A season that does have crops for this place and watering."""
    value: str  # wizard answer value to send back: Kharif | Rabi | Summer | Annual
    label: str  # in the farmer's language
    crops_available: int


class WizardSubmitResponse(BaseModel):
    recommendations: List[WizardRecommendationItem]
    recommendation_id: str
    farm_id: Optional[str] = None  # the farm this run is for; for a new farmer, the farm Agro is creating
    language: str = "en"
    # False when no crop fit: the credit was given back and nothing was saved.
    credit_used: bool = True
    # Only when no crop fit: other seasons with crops here, most crops first.
    season_suggestions: List[SeasonSuggestion] = Field(default_factory=list)
    context: Optional[WizardContext] = None


__all__ = [
    "WizardQuestionOption", "WizardQuestion", "FarmContext", "WizardQuestionsResponse",
    "WizardAnswersPayload", "WizardSubmitRequest", "WizardRecommendationItem", "WizardContext",
    "SeasonSuggestion", "WizardSubmitResponse",
]
