"""Unit tests for POST /wizard/submit — field mapping and farm-context merge."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from models.wizardModels import WizardAnswersPayload, WizardSubmitRequest


class TestMappingHelpers(IsolatedAsyncioTestCase):
    async def test_season_mapping(self):
        from routers.v2.wizard import _map_season
        self.assertEqual(_map_season("Kharif"), "KHARIF")
        self.assertEqual(_map_season("rabi"), "RABI")
        self.assertEqual(_map_season("Summer"), "ZAID")
        self.assertEqual(_map_season("Annual"), "LONGTERM")

    async def test_season_mapping_unknown_raises_value_error(self):
        from routers.v2.wizard import _map_season
        with self.assertRaises(ValueError):
            _map_season("Autumn")

    async def test_budget_mapping(self):
        from routers.v2.wizard import _map_budget
        self.assertEqual(_map_budget("low"), "B1")
        self.assertEqual(_map_budget("medium"), "B3")
        self.assertEqual(_map_budget("high"), "B4")

    async def test_irrigation_mapping(self):
        from routers.v2.wizard import _map_irrigation
        self.assertEqual(_map_irrigation(["drip", "Sprinkler"]), ["DRIP", "SPRINKLER"])

    async def test_irrigation_mapping_unknown_value_raises_value_error(self):
        # Regression for review finding Important #3: an unmapped irrigation
        # value (e.g. "Flood" — a real example value in
        # FM_frontend_api_contract_guide.md's ExistingFarmContext, not in
        # WaterMethod) must raise cleanly here, not reach
        # RecommendationRequest's Pydantic validator and crash as an
        # unhandled 500 — same contract as _map_season/_map_budget.
        from routers.v2.wizard import _map_irrigation
        with self.assertRaises(ValueError):
            _map_irrigation(["Flood"])


class TestWizardSubmit(IsolatedAsyncioTestCase):
    @patch("routers.v2.wizard.publish_event", new_callable=AsyncMock)
    @patch("routers.v2.wizard.scoring_engine")
    async def test_submit_without_farm_id_uses_payload_location(self, mock_scoring_engine, mock_publish):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse, CropRecommendation

        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(
            results=[CropRecommendation(
                crop="Tomato", family="Vegetable", horizon="SEASONAL",
                total=90, max_total=100, suitability_pct=90.0,
                factors=[],
            )],
        ))

        req = WizardSubmitRequest(
            farmId=None,
            answers=WizardAnswersPayload(
                location=[73.85, 18.52], season="Rabi", irrigation=["Drip"], budget="high",
                # previous_crop omitted -> was_fallow=True, skips the crop_manager lookup entirely
            ),
        )
        result = await submit_wizard(req, auth_payload={"upid": "usr_1"})

        self.assertEqual(len(result.recommendations), 1)
        self.assertEqual(result.recommendations[0].crop_name, "Tomato")
        mock_scoring_engine.recommend.assert_awaited_once()
        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertEqual(rec_request.lat, 18.52)
        self.assertEqual(rec_request.lon, 73.85)
        self.assertEqual(rec_request.season, "RABI")
        self.assertEqual(rec_request.budget_bucket, "B4")

    @patch("routers.v2.wizard.publish_event", new_callable=AsyncMock)
    @patch("routers.v2.wizard.agro_internal_service")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_submit_with_farm_id_uses_farm_center_when_location_omitted(
        self, mock_scoring_engine, mock_agro_service, mock_publish
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-1", "center": [73.0, 18.0], "crop": "Rice", "has_active_crop": True,
        })
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId="farm-1",
            answers=WizardAnswersPayload(season="Rabi", budget="low", irrigation=["Drip"]),
            # previous_crop omitted and not on the mocked farm -> was_fallow=True, skips crop lookup
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertEqual(rec_request.lat, 18.0)
        self.assertEqual(rec_request.lon, 73.0)

    @patch("routers.v2.wizard.publish_event", new_callable=AsyncMock)
    async def test_submit_without_location_and_without_farm_id_returns_400(self, mock_publish):
        from routers.v2.wizard import submit_wizard
        from fastapi import HTTPException

        req = WizardSubmitRequest(farmId=None, answers=WizardAnswersPayload(season="Rabi"))
        with self.assertRaises(HTTPException) as ctx:
            await submit_wizard(req, auth_payload={"upid": "usr_1"})
        self.assertEqual(ctx.exception.status_code, 400)

    @patch("routers.v2.wizard.publish_event", new_callable=AsyncMock)
    @patch("routers.v2.wizard.scoring_engine")
    async def test_kafka_publish_failure_does_not_break_response(self, mock_scoring_engine, mock_publish):
        # Review Focus: Kafka outage must not turn a working recommendation into a 500.
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))
        mock_publish.side_effect = RuntimeError("MSK unreachable")

        req = WizardSubmitRequest(
            farmId=None,
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        result = await submit_wizard(req, auth_payload={"upid": "usr_1"})  # must NOT raise
        self.assertEqual(result.recommendations, [])

    @patch("routers.v2.wizard.publish_event", new_callable=AsyncMock)
    @patch("routers.v2.wizard.scoring_engine")
    async def test_published_payload_is_valid_json_not_python_repr(self, mock_scoring_engine, mock_publish):
        # Regression for review finding Critical #1: the payload must be real
        # JSON (json.loads-able) — Agro's consumer does json.loads() on it.
        # str(python_dict) produces single-quoted Python literals (None/True)
        # instead of JSON (null/true), which raises JSONDecodeError on every
        # single message, silently dropping the whole event stream.
        import json
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId=None,
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        published_payload = mock_publish.call_args.args[1]
        parsed = json.loads(published_payload)  # must not raise
        self.assertEqual(parsed["event_type"], "CropGuideCompleted")
        self.assertEqual(parsed["user_id"], "usr_1")
