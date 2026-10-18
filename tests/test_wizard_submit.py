"""Unit tests for POST /wizard/submit — field mapping, farm-context merge,
subscription quota metering, recommendation audit trail, and rotation
baseline (DEF-02/DEF-03/DEF-04)."""
import sys
from datetime import date
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from fastapi import HTTPException
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from models.wizardModels import WizardAnswersPayload, WizardSubmitRequest


def _one_crop():
    """A scored crop: these tests exercise the normal (charged) path; an
    empty result now refunds the credit instead (see TestNoCropFits)."""
    from models.recommendationModels import CropRecommendation
    return [CropRecommendation(crop="Wheat", family="Cereal", horizon="SEASONAL", total=10, max_total=20,
                               suitability_pct=50, factors=[])]


# Every submit now reads the crop catalog (crop_manager + crop_report_manager)
# and checks user_crop_report_manager for a replayed wizard_session_id. Tests
# that don't care get empty stand-ins instead of the real (table-less) DB;
# a test's own @patch of the same name still wins for that test.
_default_patchers = []


def _empty_manager():
    manager = MagicMock()
    manager.fetch_all = AsyncMock(return_value=MagicMock(items=[], count=0))
    manager.create = AsyncMock()
    return manager


def setUpModule():
    for target in ("crop_manager", "crop_report_manager", "user_crop_report_manager"):
        patcher = patch(f"routers.v2.wizard.{target}", _empty_manager())
        patcher.start()
        _default_patchers.append(patcher)


def tearDownModule():
    for patcher in _default_patchers:
        patcher.stop()


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
        # Flowchart / TRD canonical brackets
        self.assertEqual(_map_budget("20K-40K"), "B1")
        self.assertEqual(_map_budget("40K-70K"), "B2")
        self.assertEqual(_map_budget("70K-1L"), "B3")
        self.assertEqual(_map_budget("1L-1.2L"), "B4")
        # Full labels with currency, commas, and /acre
        self.assertEqual(_map_budget("₹20,000 - ₹40,000 / acre"), "B1")
        self.assertEqual(_map_budget("₹40,000 – ₹70,000 / acre"), "B2")  # unicode en-dash
        # Questionnaire (v3) formats
        self.assertEqual(_map_budget("<₹10k"), "B1")
        self.assertEqual(_map_budget("₹10–25k"), "B2")
        self.assertEqual(_map_budget("₹25-50k"), "B3")
        self.assertEqual(_map_budget(">₹50k"), "B4")
        # Canonical bucket codes
        self.assertEqual(_map_budget("B1"), "B1")
        self.assertEqual(_map_budget("b2"), "B2")
        # Legacy 3-tier backwards compatibility
        self.assertEqual(_map_budget("low"), "B1")
        self.assertEqual(_map_budget("medium"), "B3")
        self.assertEqual(_map_budget("high"), "B4")

    async def test_budget_mapping_unknown_raises_value_error(self):
        from routers.v2.wizard import _map_budget
        with self.assertRaises(ValueError):
            _map_budget("free")

    async def test_irrigation_mapping(self):
        from routers.v2.wizard import _map_irrigation
        self.assertEqual(_map_irrigation(["drip", "Sprinkler"]), ["DRIP", "SPRINKLER"])
        # "Flood" (spec, Agro) and "Surface" (engine) are the same method (D2).
        self.assertEqual(_map_irrigation(["Flood", "surface"]), ["SURFACE"])

    async def test_irrigation_mapping_unknown_value_raises_value_error(self):
        # Regression for review finding Important #3: an unmapped irrigation
        # value must raise cleanly here, not reach RecommendationRequest's
        # Pydantic validator and crash as an unhandled 500 — same contract as
        # _map_season/_map_budget. ("Flood" used to be the example; it is now
        # an accepted alias of Surface.)
        from routers.v2.wizard import _map_irrigation
        with self.assertRaises(ValueError):
            _map_irrigation(["Borewell"])


class TestWizardSubmit(IsolatedAsyncioTestCase):
    @patch("routers.v2.wizard.crop_report_manager")
    @patch("routers.v2.wizard.user_crop_report_manager")
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_submit_without_farm_id_uses_payload_location(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager, mock_ucr_manager,
        mock_crop_report_manager,
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse, CropRecommendation

        mock_event_broker.publish = AsyncMock()
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_ucr_manager.create = AsyncMock()
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
        result = await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

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
        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-1", "center": [73.0, 18.0], "crop": "Rice", "has_active_crop": True,
        })
        # Active standing crop ("Rice") is now the rotation predecessor (DEF-04).
        mock_crop_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[MagicMock(family="Cereal", name_en="Rice")]))
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId="farm-1", subscription_id="sub_1",
            answers=WizardAnswersPayload(season="Rabi", budget="low", irrigation=["Drip"]),
        )
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

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
            await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})
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

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))
        mock_event_broker.publish = AsyncMock(side_effect=RuntimeError("MSK unreachable"))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        with self.assertRaises(RuntimeError):
            await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

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

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

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
        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_42",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

        mock_sub_service.check_quota.assert_awaited_once_with("usr_1", "sub_42")
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

        mock_sub_service.check_quota = AsyncMock(side_effect=HTTPException(status_code=480, detail="limit exceeded, subscribe now"))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_42",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        with self.assertRaises(HTTPException) as ctx:
            await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

        self.assertEqual(ctx.exception.status_code, 480)
        mock_scoring_engine.recommend.assert_not_called()

    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_scoring_failure_uses_no_credit_and_reraises(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager
    ):
        """verify reserves nothing, so nothing is confirmed or rolled back:
        a rollback would subtract a credit the farmer never spent."""
        from routers.v2.wizard import submit_wizard

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.rollback = AsyncMock(return_value=(True, {}))
        mock_scoring_engine.recommend = AsyncMock(side_effect=RuntimeError("scoring engine blew up"))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_42",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        with self.assertRaises(RuntimeError):
            await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

        mock_sub_service.rollback.assert_not_called()
        mock_sub_service.confirm.assert_not_called()


class TestWizardSubmitAuditTrail(IsolatedAsyncioTestCase):
    """DEF-03: every wizard recommendation must be logged to
    recommendation_history, same as /api/v1/recommend."""

    @patch("routers.v2.wizard.crop_report_manager")
    @patch("routers.v2.wizard.user_crop_report_manager")
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_successful_submission_writes_recommendation_history(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager, mock_ucr_manager,
        mock_crop_report_manager,
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse, CropRecommendation

        mock_event_broker.publish = AsyncMock()
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))
        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_ucr_manager.create = AsyncMock()
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
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

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
        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock(side_effect=RuntimeError("DB down"))
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_42",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        result = await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})  # must NOT raise
        self.assertEqual([r.crop_name for r in result.recommendations], ["Wheat"])


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
        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-441", "center": [73.0, 18.0],
            "crop": "Tomato", "has_active_crop": True,
            "previous_crop": "Wheat",  # must be ignored in favor of the active crop
        })
        tomato_crop = MagicMock(family="Solanaceae", name_en="Tomato")
        mock_crops_result = MagicMock(items=[tomato_crop])
        mock_crop_manager.fetch_all = AsyncMock(return_value=mock_crops_result)
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId="farm-441", subscription_id="sub_1",
            answers=WizardAnswersPayload(season="Summer", budget="low", irrigation=["Drip"]),
        )
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertEqual(rec_request.previous_crop_family, "Solanaceae")
        self.assertFalse(rec_request.was_fallow)
        # The crop catalog loads the scoring table once, then resolves names in memory.
        mock_crop_manager.fetch_all.assert_awaited_once_with(limit=0)

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
        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-2", "center": [73.0, 18.0],
            "crop": None, "has_active_crop": False, "previous_crop": "Wheat",
        })
        wheat_crop = MagicMock(family="Cereal", name_en="Wheat")
        mock_crops_result = MagicMock(items=[wheat_crop])
        mock_crop_manager.fetch_all = AsyncMock(return_value=mock_crops_result)
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId="farm-2", subscription_id="sub_1",
            answers=WizardAnswersPayload(season="Rabi", budget="low", irrigation=["Drip"]),
        )
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertEqual(rec_request.previous_crop_family, "Cereal")
        mock_crop_manager.fetch_all.assert_awaited_once_with(limit=0)


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

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(pincode="411001", season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})  # must NOT raise 400

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

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1", wizard_session_id="wiz-sess-abc",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

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

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

        published_payload = mock_event_broker.publish.call_args.args[1]
        self.assertIsNone(published_payload["data"]["wizard_session_id"])


class TestWizardSubmitReportPreHydration(IsolatedAsyncioTestCase):
    @patch("routers.v2.wizard.crop_report_manager")
    @patch("routers.v2.wizard.user_crop_report_manager")
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_successful_submission_writes_user_crop_report_and_returns_recommendation_id(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager, mock_ucr_manager,
        mock_crop_report_manager,
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse, CropRecommendation

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_ucr_manager.create = AsyncMock()
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(
            results=[
                CropRecommendation(crop="Mustard", family="Brassica", horizon="SEASONAL",
                                    total=95, max_total=100, suitability_pct=95.0, factors=[]),
                CropRecommendation(crop="Wheat", family="Cereal", horizon="SEASONAL",
                                    total=84, max_total=100, suitability_pct=84.0, factors=[]),
            ],
        ))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(
                location=[73.0, 18.0], season="Rabi", irrigation=["Drip"], farm_name="East Plot",
            ),
        )
        result = await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

        self.assertTrue(result.recommendation_id)
        mock_ucr_manager.create.assert_awaited_once()
        ucr_row = mock_ucr_manager.create.call_args.args[0]
        self.assertEqual(ucr_row.user_id, "usr_1")
        self.assertEqual(ucr_row.recommendation_id, result.recommendation_id)
        self.assertEqual(ucr_row.top_crop_name, "Mustard")
        self.assertEqual(ucr_row.top_crop_score, 95)
        self.assertEqual(ucr_row.total_recommendations, 2)
        self.assertEqual(len(ucr_row.report_data["results"]), 2)

    @patch("routers.v2.wizard.user_crop_report_manager")
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_report_prehydration_failure_does_not_break_response(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager, mock_ucr_manager
    ):
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_ucr_manager.create = AsyncMock(side_effect=RuntimeError("DB down"))
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(location=[73.0, 18.0], season="Rabi", irrigation=["Drip"]),
        )
        result = await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})  # must NOT raise
        self.assertEqual([r.crop_name for r in result.recommendations], ["Wheat"])


class TestWizardSubmitHistoryAuditColumns(IsolatedAsyncioTestCase):
    @patch("routers.v2.wizard.agro_internal_service")
    @patch("routers.v2.wizard.user_crop_report_manager")
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_history_row_includes_new_audit_columns(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager, mock_ucr_manager,
        mock_agro_service,
    ):
        # Task 1's migration adds farm_id/farm_name/livestock/current_crop_code/
        # sowing_date to recommendation_history — this pins that they're actually
        # populated, not just added to the schema and left NULL forever.
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_ucr_manager.create = AsyncMock()
        mock_agro_service.get_farm = AsyncMock(return_value={"farm_id": "farm-9", "center": [73.0, 18.0]})
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId="farm-9", subscription_id="sub_1",
            answers=WizardAnswersPayload(
                location=[73.0, 18.0], season="Rabi", irrigation=["Drip"],
                farm_name="East Plot", livestock="cattle", sowing_date="2026-10-15",
            ),
        )
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

        history_row = mock_history_manager.create.call_args.args[0]
        self.assertEqual(history_row.farm_id, "farm-9")
        self.assertEqual(history_row.farm_name, "East Plot")
        self.assertEqual(history_row.livestock, "cattle")
        # Regression for review finding Critical #1: sowing_date is a real
        # db.Date() column -- SQLAlchemy does not coerce strings, so this
        # must be a real date object, not the raw ISO string, or the write
        # raises a TypeError against a real engine (silently swallowed by
        # the try/except around this call in production).
        self.assertEqual(history_row.sowing_date, date(2026, 10, 15))

    @patch("routers.v2.wizard.user_crop_report_manager")
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_malformed_sowing_date_falls_back_to_none_not_raise(
        self, mock_scoring_engine, mock_event_broker, mock_sub_service, mock_history_manager, mock_ucr_manager
    ):
        # A malformed sowing_date must not lose the whole audit row -- it
        # falls back to None rather than raising.
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_ucr_manager.create = AsyncMock()
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(
                location=[73.0, 18.0], season="Rabi", irrigation=["Drip"], sowing_date="not-a-date",
            ),
        )
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})  # must NOT raise

        history_row = mock_history_manager.create.call_args.args[0]
        self.assertIsNone(history_row.sowing_date)

    @patch("routers.v2.wizard.user_crop_report_manager")
    @patch("routers.v2.wizard.history_manager")
    @patch("routers.v2.wizard.subscription_service")
    @patch("routers.v2.wizard.event_broker")
    @patch("routers.v2.wizard.crop_manager")
    @patch("routers.v2.wizard.scoring_engine")
    async def test_current_crop_code_uses_answers_crop_when_no_farm_and_active_crop_yes(
        self, mock_scoring_engine, mock_crop_manager, mock_event_broker, mock_sub_service,
        mock_history_manager, mock_ucr_manager
    ):
        # Regression for review finding Important #2: for a farmId-less
        # submission where the farmer reports an active crop, current_crop_code
        # must store THAT crop (answers.crop). Since the 2026-10-17 fix (P0-5)
        # the crop growing now is also the rotation baseline, so the answered
        # previous crop no longer drives scoring.
        from routers.v2.wizard import submit_wizard
        from models.recommendationModels import RecommendationResponse

        mock_sub_service.check_quota = AsyncMock()
        mock_sub_service.confirm = AsyncMock(return_value=(True, {}))
        mock_history_manager.create = AsyncMock()
        mock_event_broker.publish = AsyncMock()
        mock_ucr_manager.create = AsyncMock()
        tomato_crop = MagicMock(family="Vegetable", name_en="Tomato")
        mock_crop_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[tomato_crop]))
        mock_scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=_one_crop()))

        req = WizardSubmitRequest(
            farmId=None, subscription_id="sub_1",
            answers=WizardAnswersPayload(
                location=[73.0, 18.0], season="Rabi", irrigation=["Drip"],
                has_active_crop="Yes", crop="Tomato", sowing_date="2026-09-01", previous_crop="Wheat",
            ),
        )
        await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

        history_row = mock_history_manager.create.call_args.args[0]
        self.assertEqual(history_row.current_crop_code, "Tomato")
        # previous_crop_code records the rotation baseline: the crop in the
        # soil now, not the answered previous crop.
        self.assertEqual(history_row.previous_crop_code, "Tomato")
        rec_request = mock_scoring_engine.recommend.call_args[0][0]
        self.assertEqual(rec_request.previous_crop_family, "Vegetable")
