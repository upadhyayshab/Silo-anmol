"""Schema/manager existence and shape — no DB touched, mirrors
tests/test_farm_schema_v6_columns.py's style in the sibling Agro repo."""
import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestEventOutboxSchema(TestCase):
    def test_columns_exist(self):
        from managers.cropReportManagers import EventOutboxSchema
        cols = EventOutboxSchema.__table__.columns.keys()
        for col in ("uid", "topic", "event_key", "payload", "status", "retry_count", "last_error"):
            self.assertIn(col, cols)

    def test_status_defaults_to_pending(self):
        from managers.cropReportManagers import EventOutboxSchema
        self.assertEqual(EventOutboxSchema.__table__.columns["status"].default.arg, "PENDING")


class TestCropReportSchema(TestCase):
    def test_columns_match_seed_data_shape(self):
        from managers.cropReportManagers import CropReportSchema
        cols = CropReportSchema.__table__.columns.keys()
        for col in (
            "uid", "crop_id", "name_en", "name_local", "family", "crop_type", "seasons",
            "duration_days", "years_to_first_harvest", "suitable_soil_types", "soil_texture",
            "soil_drainage", "soil_ph_min", "soil_ph_max", "water_requirement",
            "cost_per_acre_inr", "expected_yield_per_acre", "yield_unit", "profit_potential",
            "sowing_windows", "profit_note", "weather", "risks", "tips",
            "soil_economic_benefits", "hero_image_url", "icon_url", "is_active",
            "data_source", "review_status",
        ):
            self.assertIn(col, cols)

    def test_crop_id_is_unique(self):
        from managers.cropReportManagers import CropReportSchema
        self.assertTrue(CropReportSchema.__table__.columns["crop_id"].unique)


class TestUserCropReportSchema(TestCase):
    def test_columns_exist(self):
        from managers.cropReportManagers import UserCropReportSchema
        cols = UserCropReportSchema.__table__.columns.keys()
        for col in (
            "uid", "user_id", "farm_id", "recommendation_id", "farm_name", "season",
            "language", "top_crop_id", "top_crop_name", "top_crop_score",
            "total_recommendations", "report_data",
        ):
            self.assertIn(col, cols)


class TestBrokerTypeSetting(TestCase):
    def test_defaults_to_memory(self):
        from config import get_settings
        self.assertEqual(get_settings().broker_type, "memory")
