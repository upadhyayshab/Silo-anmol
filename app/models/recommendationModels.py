from typing import Optional

from pydantic import BaseModel, Field, model_validator

from utils.constants import Season, WaterMethod, BudgetBucket


class RecommendationRequest(BaseModel):
    """Internal request model used by the scoring engine and Streamlit app."""
    lat: Optional[float] = Field(None, ge=-90, le=90, description="Latitude of the location")
    lon: Optional[float] = Field(None, ge=-180, le=180, description="Longitude of the location")
    pincode: Optional[str] = Field(None, description="6-digit Indian postal code fallback when lat/lon are omitted")
    season: Season
    water_methods: list[WaterMethod] = Field(..., min_length=1, description="Q5, select all methods the farmer has")
    soil_class: int = Field(0, ge=0, le=12, description="Q4 USDA class (1-12); 0 = No Data, skipped/neutral")
    budget_bucket: BudgetBucket
    previous_crop_family: Optional[str] = Field(
        None, description="Family of the crop grown before (e.g. 'Cereal'); required unless was_fallow=true"
    )
    was_fallow: bool = Field(False, description="True if the plot was empty/fallow before this season")
    rotation_unknown: bool = Field(
        False, description="Previous crop given but not in the crop catalog: ROTATION is left out of the score"
    )
    top_n: int = Field(5, ge=1, le=20)

    @model_validator(mode="after")
    def _check_rotation_input(self):
        if not self.was_fallow and not self.rotation_unknown and not self.previous_crop_family:
            raise ValueError("previous_crop_family is required unless was_fallow or rotation_unknown is true")
        return self


class PublicRecommendationRequest(BaseModel):
    """Public API request body — soil_class is NOT accepted here; it is auto-detected
    from lat/lon via the ISRIC raster SoilTextureLookup engine.
    previous_crop_code is the crop name as returned by the /options endpoint (e.g. 'Wheat');
    the engine resolves the crop family from the database.
    """
    subscription_id: str = Field(..., description="Active subscription id, used for usage metering")
    season: Season = Field(..., description="Planting season")
    water_methods: list[WaterMethod] = Field(..., min_length=1, description="All irrigation methods available")
    budget_bucket: BudgetBucket = Field(..., description="Per-acre spend bracket")
    previous_crop_code: Optional[str] = Field(
        None, description="Name of the crop grown before (e.g. 'Wheat'). Omit if was_fallow=true."
    )
    was_fallow: bool = Field(False, description="True if the plot was barren / fallow before this season")
    top_n: int = Field(5, ge=1, le=20, description="Number of top recommendations to return")

    @model_validator(mode="after")
    def _check_rotation_input(self):
        if not self.was_fallow and not self.previous_crop_code:
            raise ValueError("previous_crop_code is required unless was_fallow is true")
        return self


class FactorScore(BaseModel):
    factor: str
    points: int
    weight: float
    weighted: float


class CropRecommendation(BaseModel):
    crop: str
    translated_name: Optional[str] = Field(None, description="Crop name in requested language; None if lang=en")
    family: str
    horizon: str
    total: float
    max_total: float
    suitability_pct: float
    factors: list[FactorScore]


class RecommendationResponse(BaseModel):
    zone_filter_applied: bool = False
    soil_class_detected: Optional[int] = Field(None, description="USDA soil class auto-detected from coordinates")
    soil_texture_label: Optional[str] = Field(None, description="Human-readable soil texture label")
    notes: list[str] = Field(default_factory=list)
    results: list[CropRecommendation]


__all__ = [
    "RecommendationRequest", "PublicRecommendationRequest",
    "FactorScore", "CropRecommendation", "RecommendationResponse",
]
