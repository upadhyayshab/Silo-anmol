import sqlalchemy as db

from SharedBackend.managers import BaseSchema, GenericManager

# Shared Enum type objects: reusing the same instance (rather than re-declaring per column)
# means SQLAlchemy emits a single CREATE TYPE for Postgres even though multiple columns
# across tables reference it (e.g. CropSchema.cost_class and BudgetRuleSchema.cost_class).
HorizonEnum = db.Enum("SEASONAL", "ANNUAL", "PERENNIAL", name="horizon_enum")
WaterNeedEnum = db.Enum("LOW", "MEDIUM", "HIGH", name="water_need_enum")
CostClassEnum = db.Enum("LOW", "MEDIUM", "HIGH", name="cost_class_enum")
SeasonEnum = db.Enum("KHARIF", "RABI", "ZAID", "LONGTERM", name="season_enum")
WaterMethodEnum = db.Enum("RAINFED", "SURFACE", "DRIP", "SPRINKLER", name="water_method_enum")
BudgetBucketEnum = db.Enum("B1", "B2", "B3", "B4", name="budget_bucket_enum")
RotationSituationEnum = db.Enum("DIFFERENT_FAMILY", "FALLOW", "SAME_FAMILY", name="rotation_situation_enum")
ScoringFactorEnum = db.Enum("ZONE", "SEASON", "WATER", "SOIL", "BUDGET", "ROTATION", name="scoring_factor_enum")


class CropSchema(BaseSchema):
    """Sheet 2 'Crop Attributes' -- one row per crop (104 total)."""
    __tablename__ = "crops"

    code = db.Column(db.Integer, nullable=False, unique=True)
    name_en = db.Column(db.String, nullable=False, unique=True)
    name_hn = db.Column(db.String, nullable=True)
    name_kn = db.Column(db.String, nullable=True)
    name_ta = db.Column(db.String, nullable=True)
    name_te = db.Column(db.String, nullable=True)
    name_ml = db.Column(db.String, nullable=True)
    name_mr = db.Column(db.String, nullable=True)
    name_gu = db.Column(db.String, nullable=True)
    name_bn = db.Column(db.String, nullable=True)
    name_pa = db.Column(db.String, nullable=True)
    name_ur = db.Column(db.String, nullable=True)
    name_ne = db.Column(db.String, nullable=True)
    name_or = db.Column(db.String, nullable=True)
    name_as = db.Column(db.String, nullable=True)
    family = db.Column(db.String, nullable=False)
    horizon = db.Column(HorizonEnum, nullable=False)
    water_need = db.Column(WaterNeedEnum, nullable=False)
    cost_class = db.Column(CostClassEnum, nullable=False)
    # Main zones stored as a SMALLINT bitmask: zone N occupies bit (N-1).
    # e.g. zones {3,4,5} = (1<<2)|(1<<3)|(1<<4) = 0x001C = 28
    # Max 15 zones -> max value 0x7FFF = 32767 (fits SMALLINT).
    # Query: WHERE (main_zones_mask & (1 << :zone - 1)) != 0
    main_zones_mask = db.Column(db.SmallInteger, nullable=False, default=0)
    note = db.Column(db.String, nullable=True)


class CropManager(GenericManager[CropSchema]):
    pass


class CropZoneScoreSchema(BaseSchema):
    """Sheet 3 'Zone (HARD)' -- crop x zone(1-15) grid. Not yet used by the scoring
    engine (no district/pincode -> zone resolver exists), but loaded so it's a
    one-file drop-in once that mapping is available."""
    __tablename__ = "crop_zone_scores"
    __table_args__ = (db.UniqueConstraint("crop_uid", "zone"),)

    crop_uid = db.Column(db.String, db.ForeignKey("crops.uid"), nullable=False)
    zone = db.Column(db.Integer, nullable=False)
    score = db.Column(db.Integer, nullable=False)


class CropZoneScoreManager(GenericManager[CropZoneScoreSchema]):
    pass


class CropSeasonScoreSchema(BaseSchema):
    """Sheet 4 'Season (HARD)' -- crop x season grid."""
    __tablename__ = "crop_season_scores"
    __table_args__ = (db.UniqueConstraint("crop_uid", "season"),)

    crop_uid = db.Column(db.String, db.ForeignKey("crops.uid"), nullable=False)
    season = db.Column(SeasonEnum, nullable=False)
    score = db.Column(db.Integer, nullable=False)


class CropSeasonScoreManager(GenericManager[CropSeasonScoreSchema]):
    pass


class CropWaterScoreSchema(BaseSchema):
    """Sheet 5 'Water (HARD)' -- crop x irrigation method grid."""
    __tablename__ = "crop_water_scores"
    __table_args__ = (db.UniqueConstraint("crop_uid", "method"),)

    crop_uid = db.Column(db.String, db.ForeignKey("crops.uid"), nullable=False)
    method = db.Column(WaterMethodEnum, nullable=False)
    score = db.Column(db.Integer, nullable=False)


class CropWaterScoreManager(GenericManager[CropWaterScoreSchema]):
    pass


class CropSoilScoreSchema(BaseSchema):
    """Sheet 6 'Soil (score)' -- crop x USDA soil-texture class (1-12) grid.
    Class 0 ('No Data') has no row here; the engine treats it as skip/neutral."""
    __tablename__ = "crop_soil_scores"
    __table_args__ = (db.UniqueConstraint("crop_uid", "soil_class"),)

    crop_uid = db.Column(db.String, db.ForeignKey("crops.uid"), nullable=False)
    soil_class = db.Column(db.Integer, nullable=False)
    score = db.Column(db.Integer, nullable=False)


class CropSoilScoreManager(GenericManager[CropSoilScoreSchema]):
    pass


class BudgetRuleSchema(BaseSchema):
    """Sheet 8 'Budget Logic' -- farmer spend bucket x crop cost-class grid (12 rows)."""
    __tablename__ = "budget_rules"
    __table_args__ = (db.UniqueConstraint("budget_bucket", "cost_class"),)

    budget_bucket = db.Column(BudgetBucketEnum, nullable=False)
    cost_class = db.Column(CostClassEnum, nullable=False)
    score = db.Column(db.Integer, nullable=False)


class BudgetRuleManager(GenericManager[BudgetRuleSchema]):
    pass


class RotationRuleSchema(BaseSchema):
    """Sheet 10 'Rotation Logic' -- static rule, 3 rows."""
    __tablename__ = "rotation_rules"

    situation = db.Column(RotationSituationEnum, nullable=False, unique=True)
    score = db.Column(db.Integer, nullable=False)


class RotationRuleManager(GenericManager[RotationRuleSchema]):
    pass


class ScoringWeightSchema(BaseSchema):
    """Sheet 1 'Weights' -- per-factor weight, 6 rows."""
    __tablename__ = "scoring_weights"

    factor = db.Column(ScoringFactorEnum, nullable=False, unique=True)
    weight = db.Column(db.Float, nullable=False)


class ScoringWeightManager(GenericManager[ScoringWeightSchema]):
    pass


class PincodeZoneMappingSchema(BaseSchema):
    """Pincode to Agro-Climatic Zone mapping table (approx. 19,500 rows)."""
    __tablename__ = "pincode_zone_mappings"

    pincode = db.Column(db.String(10), nullable=False, unique=True)
    zone = db.Column(db.Integer, nullable=False)


class PincodeZoneMappingManager(GenericManager[PincodeZoneMappingSchema]):
    pass


class RecommendationHistorySchema(BaseSchema):
    """Audit log of every /recommend call: request params, soil-texture detection
    result, and the full ranked results, keyed by the caller's upid and created_at."""
    __tablename__ = "recommendation_history"

    upid = db.Column(db.String, nullable=False)
    subscription_id = db.Column(db.String, nullable=False)
    lat = db.Column(db.Float, nullable=True)
    lon = db.Column(db.Float, nullable=True)
    season = db.Column(SeasonEnum, nullable=False)
    water_methods = db.Column(db.JSON, nullable=False)
    budget_bucket = db.Column(BudgetBucketEnum, nullable=False)
    previous_crop_code = db.Column(db.String, nullable=True)
    was_fallow = db.Column(db.Boolean, nullable=False, default=False)
    top_n = db.Column(db.Integer, nullable=False)
    lang = db.Column(db.String(5), nullable=False)
    zone_filter_applied = db.Column(db.Boolean, nullable=False, default=False)
    soil_class_detected = db.Column(db.Integer, nullable=True)
    soil_texture_label = db.Column(db.String, nullable=True)
    results = db.Column(db.JSON, nullable=False)
    notes = db.Column(db.JSON, nullable=False)
    farm_id = db.Column(db.String(255), nullable=True)
    farm_name = db.Column(db.String(255), nullable=True)
    livestock = db.Column(db.String(255), nullable=True)
    current_crop_code = db.Column(db.String(255), nullable=True)
    sowing_date = db.Column(db.Date(), nullable=True)


class RecommendationHistoryManager(GenericManager[RecommendationHistorySchema]):
    pass


__all__ = [
    "CropSchema", "CropManager",
    "CropZoneScoreSchema", "CropZoneScoreManager",
    "CropSeasonScoreSchema", "CropSeasonScoreManager",
    "CropWaterScoreSchema", "CropWaterScoreManager",
    "CropSoilScoreSchema", "CropSoilScoreManager",
    "BudgetRuleSchema", "BudgetRuleManager",
    "RotationRuleSchema", "RotationRuleManager",
    "ScoringWeightSchema", "ScoringWeightManager",
    "PincodeZoneMappingSchema", "PincodeZoneMappingManager",
    "RecommendationHistorySchema", "RecommendationHistoryManager",
]
