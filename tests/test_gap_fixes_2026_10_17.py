"""Tests for the 2026-10-17 gap-analysis fixes (docs/2026-10-17_GAP_ANALYSIS_AND_IMPLEMENTATION_PLAN.md):
F10 Hindi codes, F11 crop catalog, F12 rotation baseline, F13 unknown crop,
F14 location checks, F15 PIN fallback, F16 questions, F17 idempotency and
farm link, F18 event payload, F19 responses and reports, F20 small fixes."""
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from models.wizardModels import WizardAnswersPayload, WizardSubmitRequest


def _manager(rows):
    manager = MagicMock()
    manager.fetch_all = AsyncMock(return_value=MagicMock(items=rows, count=len(rows)))
    manager.create = AsyncMock()
    return manager


def _report_row(crop_id, name_en, name_local=None, family="Cereal"):
    return SimpleNamespace(
        crop_id=crop_id, name_en=name_en, name_local=name_local or {}, family=family,
        crop_type="annual", seasons=["rabi"], duration_days=110, years_to_first_harvest=None,
        cost_per_acre_inr=12000, expected_yield_per_acre=8.0, yield_unit="quintal",
        profit_potential="high", sowing_windows=[], profit_note="note", weather=[], risks=[], tips=[],
        soil_economic_benefits={"title": "t", "soil": [], "economic": []},
        hero_image_url=None, icon_url=None, water_requirement="Low",
    )


CATALOG_ROWS = [
    _report_row("mustard", "Mustard", {"hi": "सरसों / राय", "mr": "मोहरी"}, "Oilseed"),
    _report_row("wheat", "Wheat", {"hi": "गेहूं"}, "Cereal"),
    _report_row("lime_lemon", "Lime / Lemon", {"hi": "नींबू"}, "Fruit"),
    _report_row("rice", "Rice (Paddy)", {"hi": "धान"}, "Cereal"),
    _report_row("fenugreek_leaf", "Fenugreek (Leaf)", {}, "Spice"),
    _report_row("fenugreek_seed", "Fenugreek (Seed)", {}, "Spice"),
]
SCORING_ROWS = [
    SimpleNamespace(name_en="Mustard", family="Oilseed"),
    SimpleNamespace(name_en="Wheat", family="Cereal"),
    SimpleNamespace(name_en="Lime/lemon", family="Fruit"),
    SimpleNamespace(name_en="Rice (paddy)", family="Cereal"),
    SimpleNamespace(name_en="Tomato", family="Vegetable"),
]


class TestLanguages(TestCase):
    """F10"""

    def test_hindi_spellings_normalize_to_hi(self):
        from utils.languages import canonical_lang, crop_name_column, normalize_lang

        for code in ("hi", "HI", "hi-IN", "hi_IN", "hn", " hn "):
            self.assertEqual(canonical_lang(code), "hi")
        self.assertIsNone(canonical_lang("xx"))
        self.assertEqual(normalize_lang("xx"), "en")
        self.assertEqual(crop_name_column("hi"), "name_hn")
        self.assertEqual(crop_name_column("mr"), "name_mr")


class TestCropCatalog(IsolatedAsyncioTestCase):
    """F11, F13: one load, any spelling, aliases, local names."""

    async def test_lookups(self):
        from services.cropCatalog import CropCatalog

        reports, scoring = _manager(CATALOG_ROWS), _manager(SCORING_ROWS)
        catalog = CropCatalog(reports, scoring)

        self.assertEqual((await catalog.find_report("Lime/lemon")).crop_id, "lime_lemon")
        self.assertEqual((await catalog.find_report("Rice (paddy)")).crop_id, "rice")
        self.assertEqual((await catalog.find_report("सरसों")).crop_id, "mustard")
        self.assertEqual((await catalog.find_report("सरसों / राय")).crop_id, "mustard")
        self.assertEqual((await catalog.find_report("Paddy")).crop_id, "rice")  # common alias
        self.assertIsNone(await catalog.find_report("Fenugreek"))  # ambiguous: leaf or seed
        self.assertIsNone(await catalog.find_report("Stevia"))
        self.assertEqual((await catalog.find_scoring("Lime / Lemon")).family, "Fruit")
        self.assertEqual((await catalog.find_scoring("गेहूं")).family, "Cereal")
        reports.fetch_all.assert_awaited_once_with(limit=0)
        scoring.fetch_all.assert_awaited_once_with(limit=0)


class TestMatchReasons(TestCase):
    """F19"""

    def test_reasons_from_factor_points(self):
        from utils.matchReasons import match_reasons

        factors = [
            {"factor": "SEASON", "points": 2}, {"factor": "WATER", "points": 1},
            {"factor": "BUDGET", "points": 2}, {"factor": "ROTATION", "points": 2},
        ]
        self.assertEqual(
            match_reasons(factors, "RABI", "Wheat"),
            ["Ideal for Rabi", "Fits your budget", "Good rotation after Wheat", "Workable with your watering method"],
        )
        self.assertEqual(match_reasons([], "RABI"), [])


class TestWizardAnswerChecks(TestCase):
    """F14 and decision D9"""

    def test_bad_locations_are_rejected(self):
        for location in ([73.8567], [], [18.5204, 73.8567], [200.0, 18.5]):
            with self.assertRaises(ValidationError):
                WizardAnswersPayload(location=location, season="Rabi")
        self.assertEqual(WizardAnswersPayload(location=[73.8567, 18.5204], season="Rabi").location, [73.8567, 18.5204])

    def test_growing_a_crop_needs_crop_and_sowing_date(self):
        with self.assertRaises(ValidationError):
            WizardAnswersPayload(season="Rabi", has_active_crop="Yes", crop="Wheat")
        with self.assertRaises(ValidationError):
            WizardAnswersPayload(season="Rabi", has_active_crop="Yes", sowing_date="2026-10-01")
        WizardAnswersPayload(season="Rabi", has_active_crop="Yes", crop="Wheat", sowing_date="2026-10-01")


class _SubmitHarness(IsolatedAsyncioTestCase):
    """Patches every collaborator of submit_wizard with a working default."""

    def setUp(self):
        from models.recommendationModels import CropRecommendation, FactorScore, RecommendationResponse

        self.results = [
            CropRecommendation(
                crop="Mustard", family="Oilseed", horizon="SEASONAL", translated_name=None,
                total=18, max_total=20, suitability_pct=90.0,
                factors=[FactorScore(factor="SEASON", points=2, weight=2, weighted=4),
                         FactorScore(factor="BUDGET", points=2, weight=1.5, weighted=3)],
            ),
            CropRecommendation(
                crop="Wheat", family="Cereal", horizon="SEASONAL", translated_name=None,
                total=14, max_total=20, suitability_pct=70.0, factors=[],
            ),
        ]
        self.scoring_engine = MagicMock()
        self.scoring_engine.recommend = AsyncMock(return_value=RecommendationResponse(
            zone_filter_applied=True, soil_texture_label="Loam", notes=["Zone 9"], results=self.results,
        ))
        self.subscription = MagicMock()
        self.subscription.check_quota = AsyncMock()
        self.subscription.confirm = AsyncMock(return_value=(True, {}))
        self.subscription.rollback = AsyncMock()
        self.broker = MagicMock(publish=AsyncMock())
        self.agro = MagicMock(get_farm=AsyncMock(return_value=None))
        self.user_reports = _manager([])
        targets = {
            "scoring_engine": self.scoring_engine, "subscription_service": self.subscription,
            "event_broker": self.broker, "agro_internal_service": self.agro,
            "history_manager": _manager([]), "user_crop_report_manager": self.user_reports,
            "crop_manager": _manager(SCORING_ROWS), "crop_report_manager": _manager(CATALOG_ROWS),
        }
        self.patchers = [patch(f"routers.v2.wizard.{name}", value) for name, value in targets.items()]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()

    async def submit(self, answers=None, farm_id=None, lang="en", session_id=None):
        from routers.v2.wizard import submit_wizard

        answers = answers or WizardAnswersPayload(location=[73.8567, 18.5204], season="Rabi", irrigation=["Drip"])
        request = WizardSubmitRequest(
            farmId=farm_id, subscription_id="sub_1", wizard_session_id=session_id, answers=answers,
        )
        return await submit_wizard(request, lang=lang, auth_payload={"upid": "usr_1"})

    def rec_request(self):
        return self.scoring_engine.recommend.call_args.args[0]

    def event(self):
        return self.broker.publish.call_args.args[1]


class TestSubmitLanguageAndResponse(_SubmitHarness):
    """F10, F19"""

    async def test_hindi_names_and_new_response_fields(self):
        result = await self.submit(lang="hi-IN")

        self.assertEqual(self.scoring_engine.recommend.call_args.kwargs["lang"], "hi")
        top = result.recommendations[0]
        self.assertEqual(top.crop_name_vernacular, "सरसों / राय")
        self.assertEqual(top.crop_id, "mustard")
        self.assertTrue(top.is_top_pick)
        self.assertFalse(result.recommendations[1].is_top_pick)
        self.assertEqual(top.match_reasons, ["Ideal for Rabi", "Fits your budget"])
        self.assertEqual(result.language, "hi")
        self.assertEqual(result.context.soil_texture_label, "Loam")


class TestSubmitRotation(_SubmitHarness):
    """F12, F13"""

    async def test_crop_growing_now_is_the_rotation_baseline(self):
        await self.submit(WizardAnswersPayload(
            location=[73.8567, 18.5204], season="Rabi", irrigation=["Drip"],
            has_active_crop="Yes", crop="Wheat", sowing_date="2026-10-01", previous_crop="Mustard",
        ))
        self.assertEqual(self.rec_request().previous_crop_family, "Cereal")  # Wheat, not Mustard

    async def test_unknown_crop_skips_rotation_instead_of_400(self):
        result = await self.submit(WizardAnswersPayload(
            location=[73.8567, 18.5204], season="Rabi", irrigation=["Drip"], previous_crop="Stevia",
        ))
        self.assertTrue(self.rec_request().rotation_unknown)
        self.assertIsNone(self.rec_request().previous_crop_family)
        self.assertEqual(len(result.recommendations), 2)

    async def test_agro_crop_name_alias_resolves(self):
        await self.submit(WizardAnswersPayload(
            location=[73.8567, 18.5204], season="Rabi", irrigation=["Drip"], previous_crop="Paddy",
        ))
        self.assertEqual(self.rec_request().previous_crop_family, "Cereal")


class TestSubmitPincodeAndFarm(_SubmitHarness):
    """F15, F20 (503/404)"""

    async def test_pincode_sent_with_coordinates_as_fallback(self):
        await self.submit(WizardAnswersPayload(
            location=[73.8567, 18.5204], pincode="411041", season="Rabi", irrigation=["Drip"],
        ))
        self.assertEqual(self.rec_request().pincode, "411041")
        self.assertEqual(self.rec_request().lat, 18.5204)

    async def test_farm_pincode_used_when_farmer_typed_none(self):
        self.agro.get_farm = AsyncMock(return_value={
            "farm_id": "f1", "center": [73.0, 18.0], "irrigation_methods": ["Drip"], "pincode": "411001",
        })
        await self.submit(WizardAnswersPayload(season="Rabi"), farm_id="f1")
        self.assertEqual(self.rec_request().pincode, "411001")
        self.assertEqual(self.rec_request().water_methods, ["DRIP"])

    async def test_agro_down_returns_503(self):
        from services.agroInternalService import AgroUnavailableError

        self.agro.get_farm = AsyncMock(side_effect=AgroUnavailableError("timeout"))
        with self.assertRaises(HTTPException) as ctx:
            await self.submit(WizardAnswersPayload(season="Rabi"), farm_id="f1")
        self.assertEqual(ctx.exception.status_code, 503)
        self.subscription.check_quota.assert_not_called()

    async def test_missing_farm_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await self.submit(WizardAnswersPayload(season="Rabi"), farm_id="f-deleted")
        self.assertEqual(ctx.exception.status_code, 404)


class TestSubmitIdempotencyAndFarmLink(_SubmitHarness):
    """F17"""

    async def test_new_farmer_gets_a_farm_id_linked_everywhere(self):
        result = await self.submit()

        self.assertTrue(result.farm_id.startswith("farms_"))
        self.assertEqual(self.event()["data"]["farm_uid"], result.farm_id)
        self.assertIsNone(self.event()["farm_id"])
        stored = self.user_reports.create.call_args.args[0]
        self.assertEqual(stored.farm_id, result.farm_id)

    async def test_same_session_is_replayed_without_charging(self):
        first = await self.submit(session_id="sess-91")
        stored = self.user_reports.create.call_args.args[0]
        self.assertEqual(stored.wizard_session_id, "sess-91")

        self.user_reports.fetch_all = AsyncMock(return_value=MagicMock(items=[stored], count=1))
        self.subscription.check_quota.reset_mock()
        self.broker.publish.reset_mock()
        second = await self.submit(session_id="sess-91")

        self.assertEqual(second.recommendation_id, first.recommendation_id)
        self.assertEqual(second.farm_id, first.farm_id)
        self.subscription.check_quota.assert_not_called()
        self.broker.publish.assert_not_called()

    async def test_event_id_is_stable_per_session(self):
        from routers.v2.wizard import _session_event_id

        self.assertEqual(_session_event_id("u", "s", "r1"), _session_event_id("u", "s", "r2"))
        self.assertEqual(_session_event_id("u", None, "r1"), "r1")


class TestEventPayload(_SubmitHarness):
    """F18"""

    async def test_event_carries_checked_values(self):
        await self.submit(WizardAnswersPayload(
            location=[73.8567, 18.5204], pincode="411041", season="Rabi", irrigation=["drip", "Surface"],
            has_active_crop="Yes", crop="Wheat", sowing_date="2026-10-01",
        ))
        data = self.event()["data"]
        self.assertEqual(data["irrigation_methods"], ["Drip", "Flood"])
        self.assertEqual(data["center"], [73.8567, 18.5204])
        self.assertTrue(data["has_active_crop"])
        self.assertEqual(data["crop"], "Wheat")
        self.assertEqual(data["sowing_date"], "2026-10-01")
        self.assertEqual(data["pincode"], "411041")
        self.assertIsNotNone(data["recommendation_id"])

    async def test_inherited_irrigation_is_still_sent(self):
        self.agro.get_farm = AsyncMock(return_value={"farm_id": "f1", "center": [73.0, 18.0], "irrigation": ["Sprinkler"]})
        await self.submit(WizardAnswersPayload(season="Rabi"), farm_id="f1")
        self.assertEqual(self.event()["data"]["irrigation_methods"], ["Sprinkler"])
        self.assertEqual(self.event()["farm_id"], "f1")


class TestScoringEngine(IsolatedAsyncioTestCase):
    """F13, F15"""

    def _engine(self):
        from utils.scoringEngine import CropScoringEngine

        engine = CropScoringEngine.__new__(CropScoringEngine)
        crop = MagicMock(uid="c1", name_en="Mustard", family="Oilseed", horizon="SEASONAL", cost_class="LOW")
        engine.crop_manager = _manager([crop])
        engine.season_manager = _manager([MagicMock(crop_uid="c1", score=2)])
        engine.water_manager = _manager([MagicMock(crop_uid="c1", score=2)])
        engine.soil_manager = _manager([])
        engine.budget_manager = _manager([MagicMock(budget_bucket="B1", cost_class="LOW", score=2)])
        engine.rotation_manager = _manager([MagicMock(situation="FALLOW", score=1)])
        engine.weight_manager = _manager([MagicMock(factor=f, weight=1) for f in
                                          ("ZONE", "SEASON", "WATER", "SOIL", "BUDGET", "ROTATION")])
        engine.zone_manager = _manager([MagicMock(crop_uid="c1", score=2)])
        engine.pincode_manager = _manager([MagicMock(zone=9)])
        engine.location_service = MagicMock(get_pincode_from_coordinates=AsyncMock(return_value=None))
        return engine

    async def test_pincode_used_when_reverse_geocoding_fails(self):
        from models.recommendationModels import RecommendationRequest

        engine = self._engine()
        response = await engine.recommend(RecommendationRequest(
            lat=18.52, lon=73.85, pincode="411041", season="RABI", water_methods=["DRIP"],
            budget_bucket="B1", was_fallow=True,
        ))
        self.assertTrue(response.zone_filter_applied)
        engine.pincode_manager.fetch_all.assert_awaited_once_with(limit=1, filters={"pincode": "411041"})

    async def test_unknown_rotation_drops_the_factor(self):
        from models.recommendationModels import RecommendationRequest

        engine = self._engine()
        response = await engine.recommend(RecommendationRequest(
            lat=None, lon=None, pincode="411041", season="RABI", water_methods=["DRIP"],
            budget_bucket="B1", rotation_unknown=True,
        ))
        factors = [f.factor for f in response.results[0].factors]
        self.assertNotIn("ROTATION", factors)
        self.assertEqual(response.results[0].max_total, 8)  # SEASON + WATER + BUDGET + ZONE, 2 points each
        self.assertIn("Previous crop not recognised; rotation factor skipped.", response.notes)


class TestReportService(IsolatedAsyncioTestCase):
    """F19: tapped crop first, real created time, sowing date, reasons."""

    def _service(self):
        from services.cropReportService import CropReportService

        run = SimpleNamespace(
            uid="ucr-1", recommendation_id="rec-1", user_id="usr_1", farm_id="farms_1", farm_name="East Plot",
            season="RABI", top_crop_name="Mustard", top_crop_score=90, total_recommendations=2,
            created_at=datetime(2026, 10, 17, 9, 30, tzinfo=timezone.utc),
            report_data={
                "results": [
                    {"crop": "Mustard", "suitability_pct": 90.0, "factors": [{"factor": "SEASON", "points": 2}]},
                    {"crop": "Wheat", "suitability_pct": 70.0, "factors": []},
                ],
                "sowing_date": "2026-10-01", "previous_crop": "Wheat",
            },
        )
        return CropReportService(crop_report_manager=_manager(CATALOG_ROWS), user_crop_report_manager=_manager([run]))

    async def test_tapped_crop_shown_first_top_pick_unchanged(self):
        report = await self._service().get_or_create_unified_report("rec-1", "usr_1", focus_crop_id="wheat")

        self.assertEqual([d.crop_id for d in report.crops], ["wheat", "mustard"])
        self.assertEqual(report.focused_crop_id, "wheat")
        self.assertEqual(report.top_crop_id, "mustard")
        self.assertTrue(report.crops[1].is_top_pick)
        self.assertEqual(report.more_for_your_farm[0].crop_id, "mustard")
        self.assertTrue(report.more_for_your_farm[0].is_top_pick)

    async def test_crop_not_in_run_is_an_error(self):
        with self.assertRaises(ValueError):
            await self._service().get_or_create_unified_report("rec-1", "usr_1", focus_crop_id="lime_lemon")

    async def test_run_fields_and_reasons(self):
        report = await self._service().get_or_create_unified_report("rec-1", "usr_1", lang="hn")

        self.assertEqual(report.created_at, "2026-10-17T09:30:00+00:00")
        self.assertEqual(report.sowing_date, "2026-10-01")
        self.assertEqual(report.language, "hi")
        self.assertEqual(report.crops[0].match_reasons, ["Ideal for Rabi"])

    async def test_reports_list_reads_hindi_for_hn_and_uppercase(self):
        for code in ("hn", "HI"):
            listing = await self._service().list_user_reports("usr_1", lang=code)
            self.assertEqual(listing.reports[0].top_crop_vernacular, "सरसों / राय")


class TestReportRoutes(IsolatedAsyncioTestCase):
    """F19 per-crop route, farmId alias; F20 /api/v6 mount."""

    @patch("routers.v2.reports.report_service")
    async def test_per_crop_route_focuses_that_crop(self, mock_service):
        from routers.v2.reports import get_recommendation_crop_report

        mock_service.get_or_create_unified_report = AsyncMock(return_value=MagicMock())
        await get_recommendation_crop_report(
            recommendation_id="rec-1", crop_id="mustard", lang="hi", farm_id=None, auth_payload={"upid": "usr_1"},
        )
        self.assertEqual(mock_service.get_or_create_unified_report.call_args.kwargs["focus_crop_id"], "mustard")

    @patch("routers.v2.reports.report_service")
    async def test_per_crop_route_404_when_crop_not_in_run(self, mock_service):
        from routers.v2.reports import get_recommendation_crop_report

        mock_service.get_or_create_unified_report = AsyncMock(side_effect=ValueError("not in run"))
        with self.assertRaises(HTTPException) as ctx:
            await get_recommendation_crop_report(
                recommendation_id="rec-1", crop_id="stevia", lang="en", farm_id=None, auth_payload={"upid": "usr_1"},
            )
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("routers.v2.reports.report_service")
    async def test_farm_id_alias(self, mock_service):
        from routers.v2.reports import list_user_reports

        mock_service.list_user_reports = AsyncMock(return_value=MagicMock())
        await list_user_reports(
            user_id="usr_1", farm_id=None, farmId="farms_1", recommendation_id=None, lang="en",
            auth_payload={"upid": "usr_1"},
        )
        self.assertEqual(mock_service.list_user_reports.call_args.kwargs["farm_id"], "farms_1")

    def test_v6_paths_mounted(self):
        import app as app_module

        paths = [r.path for r in app_module.app.routes]
        for path in ("/api/v6/wizard/questions", "/api/v6/wizard/submit",
                     "/api/v6/recommendations/{recommendation_id}/reports/{crop_id}",
                     "/api/v2/recommendations/{recommendation_id}/reports/{crop_id}"):
            self.assertIn(path, paths)


class TestLegacyRecommendLanguage(IsolatedAsyncioTestCase):
    """F10 on v1: hi accepted, truly unknown codes still 400."""

    async def test_unknown_code_still_400(self):
        from models import PublicRecommendationRequest
        from routers.v1.recommend import post_recommend

        payload = PublicRecommendationRequest(
            subscription_id="s", season="RABI", water_methods=["DRIP"], budget_bucket="B1", was_fallow=True,
        )
        with self.assertRaises(HTTPException) as ctx:
            await post_recommend(payload, lat=18.5, lon=73.8, lang="xx", auth_payload={"upid": "u"})
        self.assertEqual(ctx.exception.status_code, 400)

    @patch("routers.v1.recommend.history_manager")
    @patch("routers.v1.recommend.subscription_service")
    @patch("routers.v1.recommend.scoring_engine")
    async def test_hi_and_hn_reach_the_engine_as_hi(self, mock_engine, mock_subscription, mock_history):
        from models import PublicRecommendationRequest, RecommendationResponse
        from routers.v1.recommend import post_recommend

        mock_subscription.check_quota = AsyncMock()
        mock_subscription.confirm = AsyncMock(return_value=(True, {}))
        mock_history.create = AsyncMock()
        mock_engine.recommend = AsyncMock(return_value=RecommendationResponse(results=[]))
        payload = PublicRecommendationRequest(
            subscription_id="s", season="RABI", water_methods=["DRIP"], budget_bucket="B1", was_fallow=True,
        )
        for code in ("hi", "hn", "hi-IN"):
            await post_recommend(payload, lat=18.5, lon=73.8, lang=code, auth_payload={"upid": "u"})
            self.assertEqual(mock_engine.recommend.call_args.kwargs["lang"], "hi")


class TestAgroClient(IsolatedAsyncioTestCase):
    """F20: Agro being down is told apart from 'farm not found'."""

    @patch("services.agroInternalService.httpx.AsyncClient")
    async def test_raise_if_unavailable(self, mock_client_cls):
        from services.agroInternalService import AgroInternalService, AgroUnavailableError

        client = MagicMock(get=AsyncMock(side_effect=httpx.TimeoutException("timed out")))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        service = AgroInternalService(base_url="http://agro", shared_secret="s")

        self.assertIsNone(await service.get_farm("f1", "u1"))  # default: degrade quietly
        with self.assertRaises(AgroUnavailableError):
            await service.get_farm("f1", "u1", raise_if_unavailable=True)


class TestEventContract(_SubmitHarness):
    """F22: the event matches the shared v1 contract Agro tests against."""

    async def test_event_matches_shared_fixture(self):
        import json

        fixture = json.loads((Path(__file__).parent / "fixtures" / "crop_guide_completed.v1.json").read_text())
        await self.submit(WizardAnswersPayload(
            location=[73.8567, 18.5204], pincode="411041", season="Rabi", irrigation=["Drip", "Flood"],
            budget="<₹10k", has_active_crop="Yes", crop="Wheat", sowing_date="2026-10-01",
            previous_crop="Soybean", livestock="Cattle farm - Cows & Buffaloes", farm_name="East Plot",
        ), session_id="sess-91")
        event = self.event()

        self.assertEqual(set(event), set(fixture) - {"_comment"})
        self.assertEqual(set(event["data"]), set(fixture["data"]))
        # Values that don't depend on generated ids or time must match exactly.
        for key, value in fixture["data"].items():
            if key not in ("recommendation_id", "farm_uid"):
                self.assertEqual(event["data"][key], value, key)
        self.assertEqual(event["event_type"], fixture["event_type"])
        self.assertEqual(event["version"], fixture["version"])
