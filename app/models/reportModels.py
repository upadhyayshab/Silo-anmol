"""Pydantic models for the crop-guide report/dossier system — copied
verbatim from spec Section 5.4, under /api/v2 rather than the spec's literal
/api/v6 mount point."""
from typing import List, Optional, Dict, Literal
from pydantic import BaseModel


class WeatherMetric(BaseModel):
    param: Literal["temperature", "rainfall", "humidity"]
    title: str
    icon: str
    unit: str
    ideal_min: float
    ideal_max: float
    tolerable_min: float
    tolerable_max: float
    description: str


class CropRisk(BaseModel):
    name: str
    local_name: str
    type: Literal["fungal_disease", "insect_pest", "nutrient_deficiency", "viral_disease", "bacterial_disease", "weed", "weather"]
    severity: Literal["high", "medium", "low"]
    icon: str
    solution: str


class CropTip(BaseModel):
    title: str
    description: str
    priority: Literal["high", "medium", "low"]
    category: Literal["sowing", "protection", "harvest", "irrigation", "fertilizer", "soil"]
    icon: str


class SoilEconomicBenefits(BaseModel):
    title: str = "Soil & Economic Benefits"
    soil: List[str]
    economic: List[str]


class SowingWindow(BaseModel):
    season: str
    start: str
    end: str


class MoreForYourFarmItem(BaseModel):
    crop_id: str
    name_en: str
    name_local: Dict[str, str]
    match_score: int
    is_top_pick: bool = False
    profit_note: str


class SingleCropDossier(BaseModel):
    crop_id: str
    name_en: str
    name_local: Dict[str, str]
    match_score: int
    is_top_pick: bool
    match_reasons: List[str]
    family: str
    crop_type: Literal["annual", "perennial"]
    seasons: List[str]
    duration_days: Optional[int] = None
    years_to_first_harvest: Optional[float] = None
    cost_per_acre_inr: int
    expected_yield_per_acre: float
    yield_unit: str
    profit_potential: Literal["high", "moderate", "low"]
    profit_note: str
    sowing_windows: List[SowingWindow]
    weather: List[WeatherMetric]
    risks: List[CropRisk]
    tips: List[CropTip]
    soil_economic_benefits: SoilEconomicBenefits
    hero_image_url: Optional[str] = None
    icon_url: Optional[str] = None


class UnifiedCropReportResponse(BaseModel):
    status: str = "success"
    report_id: str
    recommendation_id: str
    user_id: str
    farm_id: Optional[str] = None
    farm_name: Optional[str] = None
    season: str
    sowing_date: Optional[str] = None
    language: str = "en"
    top_crop_id: str
    top_crop_name: str
    top_crop_score: int
    total_recommended_crops: int
    crops: List[SingleCropDossier]
    more_for_your_farm: List[MoreForYourFarmItem]
    created_at: str
    focused_crop_id: Optional[str] = None  # the crop the farmer tapped, shown first in `crops`


class RecommendedCropSummaryItem(BaseModel):
    crop_id: str
    crop_name: str
    crop_name_vernacular: str
    match_score: int
    is_top_pick: bool


class UserReportSummaryCard(BaseModel):
    report_id: str
    recommendation_id: str
    farm_id: Optional[str] = None
    farm_name: Optional[str] = None
    season: str
    top_crop_id: str
    top_crop_name: str
    top_crop_vernacular: str
    top_crop_score: int
    total_recommended_crops: int
    recommended_crops_summary: List[RecommendedCropSummaryItem]
    thumbnail_url: Optional[str] = None
    created_at: str


class UserReportsListResponse(BaseModel):
    status: str = "success"
    user_id: str
    total: int
    reports: List[UserReportSummaryCard]


__all__ = [
    "WeatherMetric", "CropRisk", "CropTip", "SoilEconomicBenefits", "SowingWindow",
    "MoreForYourFarmItem", "SingleCropDossier", "UnifiedCropReportResponse",
    "RecommendedCropSummaryItem", "UserReportSummaryCard", "UserReportsListResponse",
]
