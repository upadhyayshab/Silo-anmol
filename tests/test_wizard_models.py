"""Unit tests for the wizard Pydantic models — matches
FM_frontend_api_contract_guide.md's WizardAnswersPayload/WizardSubmitRequest
TS interfaces field-for-field."""
import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from models.wizardModels import WizardAnswersPayload, WizardSubmitRequest


class TestWizardAnswersPayload(TestCase):
    def test_all_fields_optional_except_season(self):
        payload = WizardAnswersPayload(season="Rabi")
        self.assertIsNone(payload.location)
        self.assertIsNone(payload.has_active_crop)
        self.assertIsNone(payload.irrigation)

    def test_full_payload_round_trips(self):
        payload = WizardAnswersPayload(
            location=[73.85, 18.52], season="Kharif", has_active_crop="Yes",
            crop="Tomato", sowing_date="2026-10-01", previous_crop="Wheat",
            irrigation=["Drip"], budget="high", livestock="cattle", farm_name="North Field",
        )
        self.assertEqual(payload.season, "Kharif")
        self.assertEqual(payload.irrigation, ["Drip"])


class TestWizardSubmitRequest(TestCase):
    def test_farm_id_nullable(self):
        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1", answers=WizardAnswersPayload(season="Rabi")
        )
        self.assertIsNone(req.farmId)

    def test_farm_id_present(self):
        req = WizardSubmitRequest(
            farmId="farm-1", subscription_id="sub_1", answers=WizardAnswersPayload(season="Rabi")
        )
        self.assertEqual(req.farmId, "farm-1")

    def test_subscription_id_required(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            WizardSubmitRequest(farmId=None, answers=WizardAnswersPayload(season="Rabi"))
