from enum import StrEnum


class Season(StrEnum):
    KHARIF = "KHARIF"
    RABI = "RABI"
    ZAID = "ZAID"
    LONGTERM = "LONGTERM"


class WaterMethod(StrEnum):
    RAINFED = "RAINFED"
    SURFACE = "SURFACE"
    DRIP = "DRIP"
    SPRINKLER = "SPRINKLER"


class Horizon(StrEnum):
    SEASONAL = "SEASONAL"
    ANNUAL = "ANNUAL"
    PERENNIAL = "PERENNIAL"


class CostClass(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class BudgetBucket(StrEnum):
    B1 = "B1"
    B2 = "B2"
    B3 = "B3"
    B4 = "B4"


class RotationSituation(StrEnum):
    DIFFERENT_FAMILY = "DIFFERENT_FAMILY"
    FALLOW = "FALLOW"
    SAME_FAMILY = "SAME_FAMILY"


class ScoringFactor(StrEnum):
    ZONE = "ZONE"
    SEASON = "SEASON"
    WATER = "WATER"
    SOIL = "SOIL"
    BUDGET = "BUDGET"
    ROTATION = "ROTATION"


# Informational only (Sheet 2 "Family" column) -- rotation scoring compares crop.family
# strings directly, it doesn't constrain against this list at the DB layer.
CROP_FAMILIES = [
    "Cereal", "Millet", "Pulse", "Oilseed", "Fibre", "Commercial",
    "Vegetable", "Spice", "Fruit", "Flower", "Plantation", "Fodder",
]

# USDA soil texture classes, matching reference_docs/Soil_texture_lookup_centroid (1).py's
# SOIL_TEXTURE_MAP and Scoring_Reference sheet "6. Soil (score)". Class 0 = No Data (skip, neutral).
SOIL_TEXTURE_CLASSES = {
    0: "Unclassified / No Data / Road / Water / Buildings",
    1: "Clay",
    2: "Silty Clay",
    3: "Silty Clay Loam",
    4: "Sandy Clay",
    5: "Sandy Clay Loam",
    6: "Clay Loam",
    7: "Silt",
    8: "Silt Loam",
    9: "Loam",
    10: "Sand",
    11: "Loamy Sand",
    12: "Sandy Loam",
}

__all__ = [
    "Season", "WaterMethod", "Horizon", "CostClass", "BudgetBucket",
    "RotationSituation", "ScoringFactor", "CROP_FAMILIES", "SOIL_TEXTURE_CLASSES",
]
