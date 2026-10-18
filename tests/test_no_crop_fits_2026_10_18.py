"""No crop fits (2026-10-18): when nothing passes the zone, season and water
filters, no credit is used (never confirmed, and not rolled back either: the
subscription service's verify reserves nothing, so rollback would subtract a
credit never spent), the farmer gets a reason in their language and the
seasons that do have crops there. Nothing is saved or sent to Agro."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _empty_manager():
    manager = MagicMock()
    manager.fetch_all = AsyncMock(return_value=MagicMock(items=[], count=0))
    manager.create = AsyncMock()
    return manager


class TestWizardNoCropFits(IsolatedAsyncioTestCase):
    async def _submit(self, lang="ml", farm_id=None, counts=None):
        from models.recommendationModels import RecommendationResponse
        from models.wizardModels import WizardAnswersPayload, WizardSubmitRequest
        from routers.v2 import wizard

        engine = MagicMock()
        engine.recommend = AsyncMock(return_value=RecommendationResponse(
            zone_filter_applied=True, zone=12, notes=["Zone note"], results=[],
        ))
        engine.crops_per_season = AsyncMock(return_value=counts or {"KHARIF": 9, "RABI": 3, "ZAID": 0, "LONGTERM": 5})
        subscription = MagicMock(check_quota=AsyncMock(), confirm=AsyncMock(), rollback=AsyncMock(return_value=(True, {})))
        broker = MagicMock(publish=AsyncMock())
        history, reports = _empty_manager(), _empty_manager()
        agro = MagicMock(get_farm=AsyncMock(return_value={"id": farm_id, "name": "East Plot", "center": [76.27, 9.93],
                                                           "irrigation_methods": ["Drip"]}))
        with patch.object(wizard, "scoring_engine", engine), patch.object(wizard, "subscription_service", subscription), \
                patch.object(wizard, "event_broker", broker), patch.object(wizard, "history_manager", history), \
                patch.object(wizard, "user_crop_report_manager", reports), patch.object(wizard, "agro_internal_service", agro), \
                patch.object(wizard, "crop_manager", _empty_manager()), patch.object(wizard, "crop_report_manager", _empty_manager()):
            request = WizardSubmitRequest(
                farmId=farm_id, subscription_id="sub_1", wizard_session_id="ws_1",
                answers=WizardAnswersPayload(location=[76.27, 9.93], season="Summer", irrigation=["Drip"]),
            )
            result = await wizard.submit_wizard(request, lang=lang, auth_payload={"upid": "usr_1"})
        return result, engine, subscription, broker, history, reports

    async def test_no_credit_used_and_nothing_saved_or_sent(self):
        result, engine, subscription, broker, history, reports = await self._submit()

        subscription.check_quota.assert_awaited_once()
        subscription.confirm.assert_not_awaited()
        subscription.rollback.assert_not_awaited()
        broker.publish.assert_not_awaited()
        history.create.assert_not_awaited()
        reports.create.assert_not_awaited()
        engine.crops_per_season.assert_awaited_once_with(12, ["DRIP"])

        self.assertEqual(result.recommendations, [])
        self.assertFalse(result.credit_used)
        self.assertIsNone(result.farm_id)  # no farm is created for an empty run
        self.assertEqual(result.language, "ml")

    async def test_reason_and_season_suggestions_in_the_farmers_language(self):
        from utils.farmerText import note, season_name
        from utils.wizardTranslations import OPTION_LABELS

        result, *_ = await self._submit()

        self.assertEqual(result.context.notes[0], "Zone note")
        self.assertEqual(result.context.notes[-1], note("no_crop_fits", "ml", season=season_name("ZAID", "ml")))
        self.assertEqual([(s.value, s.crops_available) for s in result.season_suggestions],
                         [("Kharif", 9), ("Annual", 5), ("Rabi", 3)])
        self.assertEqual(result.season_suggestions[0].label, OPTION_LABELS["ml"]["season"]["Kharif"])

    async def test_english_labels_and_existing_farm_kept(self):
        result, *_ = await self._submit(lang="en", farm_id="farms_abc")

        self.assertEqual(result.farm_id, "farms_abc")
        self.assertEqual(result.season_suggestions[0].label, "Kharif (Monsoon: Jun - Oct)")
        self.assertIn("Your credit was not used.", result.context.notes[-1])

    async def test_no_season_works_either(self):
        """A data gap, not the farmer's choice: no suggestions, a different
        message, credit still returned."""
        from utils.farmerText import note

        result, _, subscription, *_ = await self._submit(counts={"KHARIF": 0, "RABI": 0, "ZAID": 0, "LONGTERM": 0})
        subscription.confirm.assert_not_awaited()
        subscription.rollback.assert_not_awaited()
        self.assertFalse(result.credit_used)
        self.assertEqual(result.season_suggestions, [])
        self.assertEqual(result.context.notes[-1], note("no_crop_in_area", "ml"))

    async def test_every_place_and_watering_has_some_season_today(self):
        """With today's data a farmer can always be pointed to a season."""
        from test_crop_list_2026_10_18 import _all_scoring_data

        crops, tables = _all_scoring_data()
        zone = {(r.crop_uid, r.zone): r.score for r in tables["crop_zone_scores"]}
        season = {(r.crop_uid, r.season): r.score for r in tables["crop_season_scores"]}
        water = {(r.crop_uid, r.method): r.score for r in tables["crop_water_scores"]}
        for z in range(1, 16):
            for w in ("RAINFED", "SURFACE", "DRIP", "SPRINKLER"):
                self.assertTrue(any(zone[(c.uid, z)] and season[(c.uid, s)] and water[(c.uid, w)]
                                    for c in crops for s in ("KHARIF", "RABI", "ZAID", "LONGTERM")), (z, w))

    async def test_normal_run_still_uses_the_credit(self):
        from models.wizardModels import WizardSubmitResponse

        self.assertTrue(WizardSubmitResponse(recommendations=[], recommendation_id="r").credit_used)


class TestCropsPerSeasonOnRealData(IsolatedAsyncioTestCase):
    """The engine helper on the real 139-crop data (harness from test_crop_list)."""

    async def test_kerala_summer_is_empty_but_other_seasons_are_not(self):
        from test_crop_list_2026_10_18 import TestEngineWithNewCrops

        engine = TestEngineWithNewCrops()._engine(zone=12)
        counts = await engine.crops_per_season(12, ["DRIP"])
        self.assertEqual(counts["ZAID"], 0)
        self.assertGreater(counts["KHARIF"], 0)
        self.assertGreater(counts["LONGTERM"], 0)

    async def test_no_zone_counts_everywhere(self):
        from test_crop_list_2026_10_18 import TestEngineWithNewCrops

        engine = TestEngineWithNewCrops()._engine(zone=12)
        self.assertGreater((await engine.crops_per_season(None, ["DRIP"]))["ZAID"], 0)


class TestLegacyRecommendNoCropFits(IsolatedAsyncioTestCase):
    @patch("routers.v1.recommend.history_manager")
    @patch("routers.v1.recommend.subscription_service")
    @patch("routers.v1.recommend.scoring_engine")
    async def test_v1_uses_no_credit_either(self, mock_engine, mock_subscription, mock_history):
        from models import PublicRecommendationRequest, RecommendationResponse
        from routers.v1.recommend import post_recommend

        mock_subscription.check_quota = AsyncMock()
        mock_subscription.confirm = AsyncMock()
        mock_subscription.rollback = AsyncMock(return_value=(True, {}))
        mock_history.create = AsyncMock()
        mock_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))
        payload = PublicRecommendationRequest(
            subscription_id="s", season="ZAID", water_methods=["DRIP"], budget_bucket="B1", was_fallow=True,
        )
        response = await post_recommend(payload, lat=9.93, lon=76.27, lang="hi", auth_payload={"upid": "u"})

        mock_subscription.confirm.assert_not_awaited()
        mock_subscription.rollback.assert_not_awaited()
        self.assertEqual(response.results, [])
        self.assertIn("आपका क्रेडिट इस्तेमाल नहीं हुआ", response.notes[-1])
