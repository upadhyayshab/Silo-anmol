"""Unit tests for POST /wizard/submit — field mapping, farm-context merge,
subscription quota metering, recommendation audit trail, and rotation
baseline (DEF-02/DEF-03/DEF-04)."""
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
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_submit_without_farm_id_uses_payload_location(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse, CropRecommendation

        mock_event_broker.publish = AsyncMock()

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(
            results=[CropRecommendation(
                crop="Tomato", family="Vegetable", horizon="SEASONAL",
                total=90, max_total=100, suitability_pct=90.0,
                factors=[],
            )],
        ))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
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

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.crop_manager")
    @patch("routers.v2.wizard.agro_internal_service")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_submit_with_farm_id_uses_farm_center_when_location_omitted(
        self, mock_scoring_engine, mock_agro_service, mock_crop_manager, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_event_broker.publish = AsyncMock()
        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-1", "center": [73.0, 18.0], "crop": "Rice", "has_active_crop": True,
        })
        # Active standing crop ("Rice") is now the rotation predecessor (DEF-04).
        mock_crop_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[MagicMock(family="Cereal")]))
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId="farm-1", subscription_id="sub_1",
            answers=WizardAnswersPayload(season="Rabi", budget="low", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertEqual(rec_request.lat, 18.0)
        self.assertEqual(rec_request.lon, 73.0)

    @patch("routers.v2.wizard.event_broker")
    async def test_submit_without_location_and_without_farm_id_returns_400(self, mock_event_broker):
        from routers.v2.wizard import submit_wizard
        from fastapi import HTTPException

        mock_event_broker.publish = AsyncMock()
        req = WizardSubmitRequest(farmId=None, subscription_id="sub_1", answers=WizardAnswersPayload(season="Rabi"))
        with self.assertRaises(HTTPException) as ctx:
            await submit_wizard(req, auth_payload={"upid": "usr_1"})
        self.assertEqual(ctx.exception.status_code, 400)

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_event_broker_publish_failure_propagates_not_caught_here(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        # Review Focus (updated for Task 5): the "Kafka outage must not break
        # the response" guarantee now lives inside EventBrokerService.publish
        # itself -- it falls back to the event_outbox internally and is
        # already covered by test_event_broker_service.py's
        # test_publish_failure_falls_back_to_outbox_not_raise. The router no
        # longer wraps the call in its own try/except (that would be dead
        # code -- EventBrokerService.publish is documented to never raise),
        # so if a failure somehow still escapes it, it propagates as-is.
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))
        mock_event_broker.publish = AsyncMock(side_effect=RuntimeError("MSK unreachable"))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        with self.assertRaises(RuntimeError):
            await submit_wizard(req, auth_payload={"upid": "usr_1"})

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_published_payload_is_valid_json_not_python_repr(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        # Regression for review finding Critical #1: the payload must be real
        # JSON-serializable data -- Agro's consumer does json.loads() on it.
        # EventBrokerService.publish takes a dict now (it serializes
        # internally only in kafka mode), so this asserts directly against
        # the dict's shape rather than round-tripping through json.loads.
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        published_payload = mock_event_broker.publish.call_args.args[1]
        self.assertIsInstance(published_payload, dict)
        self.assertEqual(published_payload["event_type"], "CropGuideCompleted")
        self.assertEqual(published_payload["user_id"], "usr_1")


class TestWizardSubmitQuotaMetering(IsolatedAsyncioTestCase):
    """DEF-02: the wizard must meter usage against the same subscription
    quota /api/v1/recommend already enforces — otherwise recommendations
    via the wizard are free."""

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_quota_verified_before_scoring_and_confirmed_after_success(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_event_broker.publish = AsyncMock()
        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_42",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        mock_sub_service.verify.assert_awaited_once_with("usr_1", "sub_42")
        mock_sub_service.confirm.assert_awaited_once_with("usr_1", "sub_42")
        mock_sub_service.rollback.assert_not_called()

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_quota_exceeded_returns_480_and_skips_scoring(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from fastapi import HTTPException

        mock_sub_service.verify = AsyncMock(return_value=(False, {}))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_42",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        with self.assertRaises(HTTPException) as ctx:
            await submit_wizard(req, auth_payload={"upid": "usr_1"})

        self.assertEqual(ctx.exception.status_code, 480)
        mock_scoring_engine.recommend.assert_not_called()

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_scoring_failure_rolls_back_quota_and_reraises(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.rollback = AsyncMock(return_value=(True, {}))
        mock_scoring_engine.recommend = AsyncMock(side_effect=RuntimeError("scoring engine blew up"))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_42",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        with self.assertRaises(RuntimeError):
            await submit_wizard(req, auth_payload={"upid": "usr_1"})

        mock_sub_service.rollback.assert_awaited_once_with("usr_1", "sub_42")
        mock_sub_service.confirm.assert_not_called()


class TestWizardSubmitAuditTrail(IsolatedAsyncioTestCase):
    """DEF-03: every wizard recommendation must be logged to
    recommendation_history, same as /api/v1/recommend."""

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_successful_submission_writes_recommendation_history(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse, CropRecommendation

        mock_event_broker.publish = AsyncMock()
        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(
            zone_filter_applied=True, soil_class_detected=4, soil_texture_label="Clay Loam",
            notes=["note"],
            results=[CropRecommendation(
                crop="Tomato", family="Solanaceae", horizon="SEASONAL",
                total=90, max_total=100, suitability_pct=90.0, factors=[],
            )],
        ))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_42",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        mock_history_manager.create.assert_awaited_once()
        history_row = mock_history_manager.create.call_args.args[0]
        self.assertEqual(history_row.upid, "usr_1")
        self.assertEqual(history_row.subscription_id, "sub_42")
        self.assertEqual(history_row.lat, 18.0)
        self.assertEqual(history_row.lon, 73.0)
        self.assertEqual(history_row.season, "RABI")
        self.assertEqual(history_row.water_methods, ["DRIP"])
        self.assertTrue(history_row.zone_filter_applied)
        self.assertEqual(history_row.soil_class_detected, 4)

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_history_write_failure_does_not_break_response(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_event_broker.publish = AsyncMock()
        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock(side_effect=RuntimeError("DB down"))
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_42",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        result = await submit_wizard(req, auth_payload={"upid": "usr_1"})  # must NOT raise
        self.assertEqual(result.recommendations, [])


class TestWizardSubmitRotationBaseline(IsolatedAsyncioTestCase):
    """DEF-04: rotation scoring must use the farm's ACTIVE standing crop as
    the predecessor baseline when one exists — not just the wizard's
    previous_crop answer — without ever overwriting it (Standing Crop
    Protection stays untouched; this only affects scoring input)."""

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.crop_manager")
    @patch("routers.v2.wizard.agro_internal_service")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_active_standing_crop_used_as_rotation_predecessor(
        self, mock_scoring_engine, mock_agro_service, mock_crop_manager,
        mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_event_broker.publish = AsyncMock()
        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-441", "center": [73.0, 18.0],
            "crop": "Tomato", "has_active_crop": True,
            "previous_crop": "Wheat",  # must be ignored in favor of the active crop
        })
        tomato_crop = MagicMock(family="Solanaceae")
        mock_crops_result = MagicMock(items=[tomato_crop])
        mock_crop_manager.fetch_all = AsyncMock(return_value=mock_crops_result)
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId="farm-441", subscription_id="sub_1",
            answers=WizardAnswersPayload(season="Summer", budget="low", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertEqual(rec_request.previous_crop_family, "Solanaceae")
        self.assertFalse(rec_request.was_fallow)
        mock_crop_manager.fetch_all.assert_awaited_once_with(limit=1, filters={"name_en": "Tomato"})

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.crop_manager")
    @patch("routers.v2.wizard.agro_internal_service")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_falls_back_to_previous_crop_when_no_active_crop(
        self, mock_scoring_engine, mock_agro_service, mock_crop_manager,
        mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_event_broker.publish = AsyncMock()
        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-2", "center": [73.0, 18.0],
            "crop": None, "has_active_crop": False, "previous_crop": "Wheat",
        })
        wheat_crop = MagicMock(family="Cereal")
        mock_crops_result = MagicMock(items=[wheat_crop])
        mock_crop_manager.fetch_all = AsyncMock(return_value=mock_crops_result)
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId="farm-2", subscription_id="sub_1",
            answers=WizardAnswersPayload(season="Rabi", budget="low", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertEqual(rec_request.previous_crop_family, "Cereal")
        mock_crop_manager.fetch_all.assert_awaited_once_with(limit=1, filters={"name_en": "Wheat"})


class TestWizardSubmitPincodeAndSessionId(IsolatedAsyncioTestCase):
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_pincode_used_when_location_and_farm_both_absent(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(pincode="411001", season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})  # must NOT raise 400

        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertIsNone(rec_request.lat)
        self.assertIsNone(rec_request.lon)
        self.assertEqual(rec_request.pincode, "411001")

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_wizard_session_id_forwarded_to_broker_event(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1", wizard_session_id="wiz-sess-abc",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        published_payload = mock_event_broker.publish.call_args.args[1]
        self.assertEqual(published_payload["data"]["wizard_session_id"], "wiz-sess-abc")

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_wizard_session_id_omitted_forwards_null_not_generated(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        # Review Focus: must forward None, never silently generate a fresh
        # UUID — a generated-per-request value would defeat idempotency.
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.verify = AsyncMock(return_value=(True, {}))
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, auth_payload={"upid": "usr_1"})

        published_payload = mock_event_broker.publish.call_args.args[1]
        self.assertIsNone(published_payload["data"]["wizard_session_id"])
