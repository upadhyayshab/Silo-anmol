"""Regression tests for the issues found in the 2026-10-17 live QA run on dev
(docs/2026-10-17_CROP_QA_REPORT.md, C1-C12 and L2)."""
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    async def fetch_all(self, limit=0, **kwargs):
        return NS(items=self.rows, count=len(self.rows))


def _catalog():
    from services.cropCatalog import CropCatalog

    reports = [
        NS(crop_id="mustard", name_en="Mustard", name_local={"hi": "सरसों / राई", "mr": "मोहरी"}),
        NS(crop_id="lime_lemon", name_en="Lime / Lemon", name_local={"hi": "नींबू"}),
        NS(crop_id="chilli", name_en="Chilli", name_local={}),
        NS(crop_id="lucerne", name_en="Lucerne (Alfalfa)", name_local={}),
        NS(crop_id="rajma", name_en="Rajma (Kidney Bean)", name_local={}),
    ]
    scoring = [
        NS(name_en="Mustard", family="Brassica", name_hn="सरसों", name_kn="ಸಾಸಿವೆ", name_ta="கடுகு", name_mr="मोहरी"),
        NS(name_en="Lime/lemon", family="Citrus", name_hn="नींबू", name_kn="ನಿಂಬೆ"),
        NS(name_en="Chilli", family="Solanaceae", name_kn="ಮೆಣಸಿನಕಾಯಿ"),
        NS(name_en="Lucerne", family="Legume", name_kn="ಕುದುರೆ ಮೆಂತ್ಯ"),
        NS(name_en="Rajma", family="Legume", name_kn="ರಾಜ್ಮಾ"),
    ]
    return CropCatalog(_Rows(reports), _Rows(scoring))


class TestCatalogLanguagesAndAgroNames(IsolatedAsyncioTestCase):
    async def test_kannada_and_tamil_names_resolve(self):
        catalog = _catalog()
        self.assertEqual((await catalog.find_report("ಸಾಸಿವೆ")).crop_id, "mustard")
        self.assertEqual((await catalog.find_scoring("ಸಾಸಿವೆ")).family, "Brassica")
        self.assertEqual((await catalog.find_report("கடுகு")).crop_id, "mustard")

    async def test_local_names_cover_every_language_with_curated_hi_mr_first(self):
        catalog = _catalog()
        mustard = await catalog.find_report("mustard")
        names = await catalog.local_names(mustard)
        self.assertEqual(names["kn"], "ಸಾಸಿವೆ")
        self.assertEqual(names["ta"], "கடுகு")
        self.assertEqual(names["hi"], "सरसों / राई")  # crops.json wins over the table's spelling
        self.assertEqual(await catalog.local_name(mustard, "kn"), "ಸಾಸಿವೆ")

    async def test_agro_crop_search_spellings(self):
        catalog = _catalog()
        for name, crop_id in (("Lemon", "lime_lemon"), ("Chili, fresh", "chilli"), ("Chili, dry ", "chilli"),
                              ("Alfalfa", "lucerne"), ("Beans, dry ", "rajma"), ("beans,_dry_", "rajma")):
            self.assertEqual((await catalog.find_report(name)).crop_id, crop_id, name)


class TestReportServiceNames(IsolatedAsyncioTestCase):
    async def test_dossier_and_list_use_all_languages(self):
        from services.cropReportService import CropReportService

        catalog = _catalog()
        master = NS(
            crop_id="mustard", name_en="Mustard", name_local={"hi": "सरसों / राई"}, family="Brassica",
            crop_type="annual", seasons=["rabi"], duration_days=110, years_to_first_harvest=None,
            cost_per_acre_inr=12000, expected_yield_per_acre=8, yield_unit="quintal", profit_potential="high",
            profit_note="n", sowing_windows=[], weather=[], risks=[], tips=[],
            soil_economic_benefits={"soil": [], "economic": []}, hero_image_url=None, icon_url=None,
        )
        catalog.find_report = AsyncMock(return_value=master)
        catalog._ensure_loaded = AsyncMock()
        catalog._names_by_crop_id = {"mustard": {"kn": "ಸಾಸಿವೆ", "hi": "सरसों"}}
        service = CropReportService(MagicMock(), MagicMock(), catalog=catalog)

        dossier = await service.get_master_report("mustard", lang="kn")
        self.assertEqual(dossier.name_local["kn"], "ಸಾಸಿವೆ")
        self.assertEqual(dossier.name_local["hi"], "सरसों / राई")

    async def test_report_list_is_newest_first(self):
        from services.cropReportService import CropReportService

        runs = MagicMock()
        runs.fetch_all = AsyncMock(return_value=NS(items=[]))
        service = CropReportService(MagicMock(), runs, catalog=_catalog())
        await service.list_user_reports(user_id="u1")
        # SharedBackend: "+" sorts descending.
        self.assertEqual(runs.fetch_all.call_args.kwargs["sorts"], ["+created_at"])


class TestWizardSubmitOncePerSession(IsolatedAsyncioTestCase):
    async def test_replay_check_and_submit_run_inside_the_session_lock(self):
        import routers.v2.wizard as W
        from models.wizardModels import WizardSubmitRequest

        calls = []

        @asynccontextmanager
        async def lock(key):
            calls.append(("lock", key))
            yield
            calls.append(("unlock", key))

        async def check(**kwargs):
            calls.append(("check",))
            return NS(items=[])

        async def submit(payload, lang_code, upid):
            calls.append(("submit",))
            return "result"

        payload = WizardSubmitRequest(subscription_id="s1", wizard_session_id="sess-1",
                                      answers={"season": "Kharif", "location": [73.8, 18.5]})
        with patch.object(W, "_session_lock", lock), patch.object(W, "_submit", submit), \
                patch.object(W.user_crop_report_manager, "fetch_all", AsyncMock(side_effect=check)):
            result = await W.submit_wizard(payload=payload, lang="en", auth_payload={"upid": "u1"})

        self.assertEqual(result, "result")
        key = "wizard_submit:u1:sess-1"
        self.assertEqual(calls, [("lock", key), ("check",), ("submit",), ("unlock", key)])

    async def test_postgres_lock_uses_a_transaction_scoped_advisory_lock(self):
        import routers.v2.wizard as W

        conn = AsyncMock()
        begin = MagicMock()
        begin.__aenter__ = AsyncMock(return_value=conn)
        begin.__aexit__ = AsyncMock(return_value=False)
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        engine.begin = MagicMock(return_value=begin)
        with patch.object(W, "engine", engine):
            async with W._session_lock("wizard_submit:u1:s"):
                pass
        sql, params = conn.execute.call_args.args
        self.assertIn("pg_advisory_xact_lock", str(sql))
        self.assertEqual(params, {"key": "wizard_submit:u1:s"})

    async def test_agro_old_canal_name_is_flood(self):
        from routers.v2.wizard import _map_irrigation

        self.assertEqual(_map_irrigation(["canal"]), ["SURFACE"])


class TestOpenRoutes(IsolatedAsyncioTestCase):
    async def test_raw_scoring_route_needs_the_admin_key(self):
        from routers.v1.recommendations import router
        from utils.dependencies import master_key_dependency

        self.assertIn(master_key_dependency, [d.dependency for d in router.dependencies])

    async def test_example_route_is_not_mounted(self):
        from routers.v1 import router

        self.assertFalse([r.path for r in router.routes if r.path.startswith("/examples")])

    async def test_missing_token_is_401_not_422(self):
        from dependencies.auth import require_jwt

        with self.assertRaises(HTTPException) as ctx:
            await require_jwt(None)
        self.assertEqual(ctx.exception.status_code, 401)


class TestCropListOrder(IsolatedAsyncioTestCase):
    @patch("routers.v1.crops.crop_manager")
    async def test_crop_list_is_a_to_z(self, mock_crop_manager):
        from routers.v1.crops import list_crops

        mock_crop_manager.fetch_all = AsyncMock(return_value=NS(items=[]))
        try:
            await list_crops()
        except Exception:
            pass
        # SharedBackend: "-" sorts ascending.
        self.assertEqual(mock_crop_manager.fetch_all.call_args.kwargs["sorts"], ["-name_en"])


class TestSoil(IsolatedAsyncioTestCase):
    async def test_soil_endpoint_503_when_raster_unavailable(self):
        from routers.v1 import soil

        with patch.object(soil, "get_soil_texture_lookup", side_effect=RuntimeError("no AWS credentials")):
            with self.assertRaises(HTTPException) as ctx:
                await soil.get_soil_texture(lat=18.5, lon=73.8, lang="en")
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_empty_pixel_uses_most_common_nearby_class(self):
        from utils.soilTexture import _nearby_soil_class

        block = np.array([[0, 0, 6, 6, 0], [0, 0, 6, 9, 0], [0, 0, 0, 0, 0], [0, 99, 0, 0, 0], [0, 0, 0, 0, 0]])
        ds = MagicMock(height=100, width=100, nodata=None)
        ds.read = MagicMock(return_value=block)
        self.assertEqual(_nearby_soil_class(ds, 50, 50, {6: "Clay Loam", 9: "Loam"}), 6)
        ds.read = MagicMock(return_value=np.zeros((5, 5), dtype=int))
        self.assertIsNone(_nearby_soil_class(ds, 50, 50, {6: "Clay Loam"}))

    async def test_soil_failure_note_has_no_technical_detail(self):
        from utils.scoringEngine import CropScoringEngine, SOIL_UNAVAILABLE_NOTE
        import utils.scoringEngine as engine_module

        notes: list[str] = []
        with patch.object(engine_module, "get_soil_texture_lookup", side_effect=RuntimeError("AWS_SECRET_ACCESS_KEY not defined")):
            soil_class = await CropScoringEngine._detect_soil_class(MagicMock(), 18.5, 73.8, notes)
        self.assertEqual(soil_class, 0)
        self.assertEqual(notes, [SOIL_UNAVAILABLE_NOTE])


class TestSeedOnStartup(IsolatedAsyncioTestCase):
    async def test_seed_failure_is_logged_not_fatal(self):
        import app as app_module

        manager = MagicMock()
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        session_cm.__aexit__ = AsyncMock(return_value=False)
        manager.session_factory = MagicMock(return_value=session_cm)
        with patch.object(app_module, "CropReportManager", return_value=manager), \
                patch.object(app_module, "seed_crop_reports", AsyncMock(side_effect=RuntimeError("db down"))):
            await app_module._seed_crop_reports()  # must not raise

    async def test_seed_runs_with_the_bundled_catalog(self):
        import app as app_module

        self.assertTrue(app_module.CROPS_FILE.exists())
        manager = MagicMock()
        session = AsyncMock()
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=False)
        manager.session_factory = MagicMock(return_value=session_cm)
        seed = AsyncMock(return_value=104)
        with patch.object(app_module, "CropReportManager", return_value=manager), patch.object(app_module, "seed_crop_reports", seed):
            await app_module._seed_crop_reports()
        self.assertEqual(seed.call_args.args[2], str(app_module.CROPS_FILE))
        session.commit.assert_awaited_once()


class TestSubscriptionChecksMatchFarmMonitoring(IsolatedAsyncioTestCase):
    """check_quota: the farmer's own, not-cancelled subscription with a
    crop-advice credit, same checks as Farm Monitoring's report quota."""

    def _service(self, active, verify=(200, {"allowed": True, "remaining": 3})):
        from services.subscriptionService import SubscriptionService

        service = SubscriptionService()
        service.base_url = "https://subscription.test"
        service._post = AsyncMock(side_effect=[active, verify])
        return service

    async def _status(self, service, subscription_id="subscriptions_mine"):
        try:
            await service.check_quota("u1", subscription_id)
        except HTTPException as exc:
            return exc.status_code
        return 200

    async def test_own_active_subscription_with_credit_passes(self):
        service = self._service((200, {"items": [{"uid": "subscriptions_mine", "subscription_status": "ACTIVE"}]}))
        self.assertEqual(await self._status(service), 200)
        self.assertEqual(service._post.call_args_list[1].kwargs["json"]["action"], "crop_advice")

    async def test_someone_elses_or_no_subscription_is_404(self):
        mine = (200, {"items": [{"uid": "subscriptions_mine", "subscription_status": "ACTIVE"}]})
        self.assertEqual(await self._status(self._service(mine), "subscriptions_theirs"), 404)
        self.assertEqual(await self._status(self._service((200, {"items": []}))), 404)

    async def test_cancelled_subscription_is_400(self):
        service = self._service((200, {"items": [{"uid": "subscriptions_mine", "subscription_status": "CANCELLED"}]}))
        self.assertEqual(await self._status(service), 400)

    async def test_no_credit_left_is_the_480_paywall(self):
        active = (200, {"items": [{"uid": "subscriptions_mine", "subscription_status": "ACTIVE"}]})
        self.assertEqual(await self._status(self._service(active, (200, {"allowed": False, "remaining": 0}))), 480)
        self.assertEqual(await self._status(self._service(active, (480, {"detail": "No subscription found"}))), 480)

    async def test_subscription_service_down_is_503_not_a_paywall(self):
        active = (200, {"items": [{"uid": "subscriptions_mine", "subscription_status": "ACTIVE"}]})
        self.assertEqual(await self._status(self._service((None, {}))), 503)
        self.assertEqual(await self._status(self._service((502, {}))), 503)
        self.assertEqual(await self._status(self._service(active, (None, {}))), 503)
        service = self._service(active)
        service.base_url = ""
        self.assertEqual(await self._status(service), 503)

    async def test_legacy_recommend_uses_the_same_check(self):
        import inspect
        import routers.v1.recommend as recommend

        self.assertIn("subscription_service.check_quota(", inspect.getsource(recommend.post_recommend))


class TestFreePlan(IsolatedAsyncioTestCase):
    """Every farmer's FREE subscription (crop_advices_limit 1) gives one free
    recommendation; the subscription service counts it, check_quota must not
    block it."""

    def _service(self, items, verify):
        from services.subscriptionService import SubscriptionService

        service = SubscriptionService()
        service.base_url = "https://subscription.test"
        service._post = AsyncMock(side_effect=[(200, {"items": items}), verify])
        return service

    async def test_unused_free_credit_passes_and_used_one_is_the_paywall(self):
        free = [{"uid": "subscriptions_free", "subscription_status": "FREE"}]
        await self._service(free, (200, {"allowed": True, "remaining": 1})).check_quota("u1", "subscriptions_free")
        with self.assertRaises(HTTPException) as ctx:
            await self._service(free, (200, {"allowed": False, "remaining": 0})).check_quota("u1", "subscriptions_free")
        self.assertEqual(ctx.exception.status_code, 480)

    async def test_free_and_paid_plan_together_charge_the_one_sent(self):
        items = [{"uid": "subscriptions_free", "subscription_status": "FREE"},
                 {"uid": "subscriptions_paid", "subscription_status": "ACTIVE"}]
        service = self._service(items, (200, {"allowed": True, "remaining": 9}))
        await service.check_quota("u1", "subscriptions_paid")
        self.assertEqual(service._post.call_args_list[1].kwargs["json"]["subscription_id"], "subscriptions_paid")


class TestInvalidTokenMessage(IsolatedAsyncioTestCase):
    async def test_invalid_token_detail_is_generic(self):
        from dependencies.auth import require_jwt

        with patch("dependencies.auth.get_settings", return_value=MagicMock(jwt_secret="s")):
            with self.assertRaises(HTTPException) as ctx:
                await require_jwt("not-a-jwt")
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (401, "Invalid token"))
