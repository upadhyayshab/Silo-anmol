"""New, additive schemas for the crop-guide spec's event outbox and
report/dossier subsystem. Kept in their own file — cropManagers.py already
holds the scoring-table managers and is large enough."""
import sqlalchemy as db
from SharedBackend.managers import BaseSchema, GenericManager


class EventOutboxSchema(BaseSchema):
    """Transactional-outbox fallback for Channel 2 (spec Section 3.3 Scenario 6B):
    when the direct Kafka publish fails, the event is parked here instead
    of being dropped, and a standalone replayer (Task 2/3) retries it."""
    __tablename__ = "event_outbox"

    topic = db.Column(db.String(128), nullable=False)
    event_key = db.Column(db.String(128), nullable=True)
    payload = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(32), nullable=False, default="PENDING")
    retry_count = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text, nullable=True)


class EventOutboxManager(GenericManager[EventOutboxSchema]):
    pass


class CropReportSchema(BaseSchema):
    """Master agronomic dossier catalog — one row per crop, seeded from the
    spec's crops.json (104 crops). `crop_id` is the natural-key slug from
    that file (e.g. 'amla'); `uid` (from BaseSchema) stays the real PK."""
    __tablename__ = "crop_reports"

    crop_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    name_en = db.Column(db.String(128), nullable=False, index=True)
    name_local = db.Column(db.JSON, nullable=False)
    family = db.Column(db.String(64), nullable=False)
    crop_type = db.Column(db.String(32), nullable=False)
    seasons = db.Column(db.JSON, nullable=False)
    duration_days = db.Column(db.Integer, nullable=True)
    years_to_first_harvest = db.Column(db.Numeric(4, 2), nullable=True)
    suitable_soil_types = db.Column(db.JSON, nullable=False)
    soil_texture = db.Column(db.JSON, nullable=False)
    soil_drainage = db.Column(db.String(64), nullable=False)
    soil_ph_min = db.Column(db.Numeric(3, 1), nullable=False)
    soil_ph_max = db.Column(db.Numeric(3, 1), nullable=False)
    water_requirement = db.Column(db.String(32), nullable=False)
    cost_per_acre_inr = db.Column(db.Integer, nullable=False)
    expected_yield_per_acre = db.Column(db.Numeric(10, 2), nullable=False)
    yield_unit = db.Column(db.String(32), nullable=False)
    profit_potential = db.Column(db.String(32), nullable=False)
    sowing_windows = db.Column(db.JSON, nullable=False)
    profit_note = db.Column(db.Text, nullable=False)
    weather = db.Column(db.JSON, nullable=False)
    risks = db.Column(db.JSON, nullable=False)
    tips = db.Column(db.JSON, nullable=False)
    soil_economic_benefits = db.Column(db.JSON, nullable=False)
    hero_image_url = db.Column(db.String(512), nullable=True)
    icon_url = db.Column(db.String(512), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    data_source = db.Column(db.Text, nullable=True)
    review_status = db.Column(db.String(32), nullable=False, default="ai_draft")


class CropReportManager(GenericManager[CropReportSchema]):
    pass


class UserCropReportSchema(BaseSchema):
    """One row per recommendation run (not per crop) — report_data holds the
    full hydrated dossier list for every ranked crop in that run, per TRD
    Section 5.4 UnifiedCropReportResponse.crops / Section 2.1's 'user_crop_reports'."""
    __tablename__ = "user_crop_reports"

    user_id = db.Column(db.String(64), nullable=False, index=True)
    farm_id = db.Column(db.String(64), nullable=True, index=True)
    recommendation_id = db.Column(db.String(64), nullable=False, index=True)
    # Client-generated id of one wizard attempt: a resubmission with the same
    # id replays this run instead of charging and creating a farm again.
    wizard_session_id = db.Column(db.String(128), nullable=True, index=True)
    farm_name = db.Column(db.String(128), nullable=True)
    season = db.Column(db.String(32), nullable=False)
    language = db.Column(db.String(10), nullable=False, default="en")
    top_crop_id = db.Column(db.String(64), nullable=False)
    top_crop_name = db.Column(db.String(128), nullable=False)
    top_crop_score = db.Column(db.Integer, nullable=False)
    total_recommendations = db.Column(db.Integer, nullable=False, default=1)
    report_data = db.Column(db.JSON, nullable=False)


class UserCropReportManager(GenericManager[UserCropReportSchema]):
    pass


__all__ = [
    "EventOutboxSchema", "EventOutboxManager",
    "CropReportSchema", "CropReportManager",
    "UserCropReportSchema", "UserCropReportManager",
]
