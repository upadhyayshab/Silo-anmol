"""Credit ledger: every Crop Guide path that can charge a farmer, run against
a fake subscription service that counts exactly like the real one
(ks-subs-dashboard app/routers/v1/usage.py, read 2026-10-18):

- verify   read-only: allowed = used + 1 <= limit
- confirm  used + 1, atomically, refused (480) past the limit
- rollback used - 1, refused (409) at 0

Each test checks the final counter, so a path that charges twice, charges
for nothing, hands out a free credit or delivers an unpaid result fails here.
Our real SubscriptionService client is used; only its HTTP call is faked.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class FakeSubscriptionServer:
    def __init__(self, used=0, limit=1, status="FREE", down=False):
        self.used, self.limit, self.status, self.down = used, limit, status, down
        self.calls: list[str] = []

    async def post(self, path, upid, json=None):
        verb = path.rsplit("/", 1)[-1]
        self.calls.append(verb)
        if self.down and verb in ("confirm",):
            return None, {"error": "connection refused"}
        if path == "/api/v1/subscriptions/active":
            return 200, {"items": [{"uid": "sub_1", "subscription_status": self.status}]}
        if verb == "verify":
            return 200, {"allowed": self.used + 1 <= self.limit, "remaining": float(self.limit - self.used),
                         "used": self.used, "limit": self.limit}
        if verb == "confirm":
            if self.used + 1 > self.limit:
                return 480, {"detail": "Usage limit exceeded"}
            self.used += 1
            return 200, {"status": "consumed", "new_value": self.used}
        if verb == "rollback":
            if self.used < 1:
                return 409, {"detail": "Rollback failed"}
            self.used -= 1
            return 200, {"status": "rolled_back", "new_value": self.used}
        raise AssertionError(f"unexpected call {path}")


class MemoryReports:
    """user_crop_report_manager stand-in that keeps what's saved, so replays work."""

    def __init__(self):
        self.rows = []

    async def fetch_all(self, limit=0, filters=None, **kwargs):
        rows = [r for r in self.rows if all(getattr(r, k, None) == v for k, v in (filters or {}).items())]
        return SimpleNamespace(items=rows[:limit] if limit else rows, count=len(rows))

    async def create(self, row, session=None):
        self.rows.append(row)
        return row


def _empty_manager():
    manager = MagicMock()
    manager.fetch_all = AsyncMock(return_value=MagicMock(items=[], count=0))
    manager.create = AsyncMock()
    return manager


def _crops():
    from models.recommendationModels import CropRecommendation, RecommendationResponse
    return RecommendationResponse(results=[CropRecommendation(
        crop="Wheat", family="Cereal", horizon="SEASONAL", total=10, max_total=20, suitability_pct=50, factors=[])])


def _no_crops():
    from models.recommendationModels import RecommendationResponse
    return RecommendationResponse(zone=12, results=[])


class TestWizardLedger(IsolatedAsyncioTestCase):
    def setUp(self):
        from routers.v2 import wizard

        self.wizard = wizard
        self.server = FakeSubscriptionServer()
        self.reports = MemoryReports()
        self.broker = MagicMock(publish=AsyncMock())
        self.engine = MagicMock(recommend=AsyncMock(return_value=_crops()),
                                crops_per_season=AsyncMock(return_value={"KHARIF": 3, "RABI": 0, "ZAID": 0, "LONGTERM": 0}))
        targets = {"scoring_engine": self.engine, "event_broker": self.broker, "user_crop_report_manager": self.reports,
                   "history_manager": _empty_manager(), "crop_manager": _empty_manager(),
                   "crop_report_manager": _empty_manager()}
        self.patchers = [patch.object(wizard, name, value) for name, value in targets.items()]
        self.patchers.append(patch.object(wizard.subscription_service, "_post", self.server.post))
        self.patchers.append(patch.object(wizard.subscription_service, "base_url", "http://subscriptions"))
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()

    async def _submit(self, session_id="ws_1", season="Rabi"):
        from models.wizardModels import WizardAnswersPayload, WizardSubmitRequest

        request = WizardSubmitRequest(subscription_id="sub_1", wizard_session_id=session_id,
                                      answers=WizardAnswersPayload(location=[75.85, 30.9], season=season, irrigation=["Drip"]))
        return await self.wizard.submit_wizard(request, lang="en", auth_payload={"upid": "usr_1"})

    async def test_result_delivered_counts_once(self):
        result = await self._submit()
        self.assertEqual(len(result.recommendations), 1)
        self.assertTrue(result.credit_used)
        self.assertEqual(self.server.used, 1)

    async def test_retry_with_same_session_counts_once(self):
        first = await self._submit("ws_1")
        second = await self._submit("ws_1")
        self.assertEqual(first.recommendation_id, second.recommendation_id)
        self.assertEqual(self.server.used, 1)
        self.engine.recommend.assert_awaited_once()

    async def test_no_crop_fits_counts_nothing(self):
        self.engine.recommend = AsyncMock(return_value=_no_crops())
        result = await self._submit(season="Summer")
        self.assertFalse(result.credit_used)
        self.assertEqual(self.server.used, 0)
        self.assertNotIn("rollback", self.server.calls)

    async def test_then_another_season_counts_once(self):
        self.engine.recommend = AsyncMock(side_effect=[_no_crops(), _crops()])
        await self._submit("ws_1", season="Summer")
        result = await self._submit("ws_1", season="Kharif")
        self.assertEqual(len(result.recommendations), 1)
        self.assertEqual(self.server.used, 1)

    async def test_engine_crash_counts_nothing(self):
        self.engine.recommend = AsyncMock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            await self._submit()
        self.assertEqual(self.server.used, 0)
        self.assertNotIn("rollback", self.server.calls)

    async def test_no_credit_left_is_refused_before_any_work(self):
        self.server.used = 1
        with self.assertRaises(HTTPException) as ctx:
            await self._submit()
        self.assertEqual(ctx.exception.status_code, 480)
        self.engine.recommend.assert_not_awaited()
        self.assertEqual(self.server.used, 1)

    async def test_last_credit_used_elsewhere_meanwhile_delivers_nothing(self):
        """Two phones, one credit left: both pass the check; the second
        confirm is refused, so its farmer must get the paywall, not a free
        result (and nothing is saved or sent to Agro for it)."""
        async def other_device_uses_the_credit(*args, **kwargs):
            self.server.used = self.server.limit
            return _crops()

        self.engine.recommend = AsyncMock(side_effect=other_device_uses_the_credit)
        with self.assertRaises(HTTPException) as ctx:
            await self._submit()
        self.assertEqual(ctx.exception.status_code, 480)
        self.assertEqual(self.server.used, 1)  # not 2
        self.assertEqual(self.reports.rows, [])
        self.broker.publish.assert_not_awaited()

    async def test_subscription_service_down_at_confirm_still_delivers(self):
        """Our dependency failing isn't the farmer's fault: the result is
        given (and logged); the counter can't be updated."""
        self.server.down = True
        result = await self._submit()
        self.assertEqual(len(result.recommendations), 1)
        self.assertEqual(self.server.used, 0)

    async def test_paid_plan_counts_every_delivered_run(self):
        self.server.limit, self.server.status = 10, "ACTIVE"
        self.engine.recommend = AsyncMock(side_effect=[_crops(), _no_crops(), _crops(), RuntimeError("boom"), _crops()])
        for i, season in enumerate(["Rabi", "Summer", "Kharif", "Rabi", "Rabi"]):
            try:
                await self._submit(f"ws_{i}", season=season)
            except RuntimeError:
                pass
        self.assertEqual(self.server.used, 3)  # 3 delivered; empty and crashed runs free
        self.assertNotIn("rollback", self.server.calls)


class TestLegacyRecommendLedger(IsolatedAsyncioTestCase):
    async def _recommend(self, server, engine_result):
        from models import PublicRecommendationRequest
        from routers.v1 import recommend

        engine = MagicMock(recommend=AsyncMock(side_effect=engine_result if callable(engine_result) else None,
                                               return_value=None if callable(engine_result) else engine_result))
        with patch.object(recommend, "scoring_engine", engine), patch.object(recommend, "history_manager", _empty_manager()), \
                patch.object(recommend.subscription_service, "_post", server.post), \
                patch.object(recommend.subscription_service, "base_url", "http://subscriptions"):
            payload = PublicRecommendationRequest(subscription_id="sub_1", season="RABI", water_methods=["DRIP"],
                                                  budget_bucket="B1", was_fallow=True)
            return await recommend.post_recommend(payload, lat=30.9, lon=75.85, lang="en", auth_payload={"upid": "usr_1"})

    async def test_delivered_counts_once_empty_counts_nothing(self):
        server = FakeSubscriptionServer(limit=10, status="ACTIVE")
        await self._recommend(server, _crops())
        await self._recommend(server, _no_crops())
        self.assertEqual(server.used, 1)
        self.assertNotIn("rollback", server.calls)

    async def test_last_credit_used_elsewhere_meanwhile_delivers_nothing(self):
        server = FakeSubscriptionServer()

        async def other_device_uses_the_credit(*args, **kwargs):
            server.used = server.limit
            return _crops()

        with self.assertRaises(HTTPException) as ctx:
            await self._recommend(server, other_device_uses_the_credit)
        self.assertEqual(ctx.exception.status_code, 480)
        self.assertEqual(server.used, 1)
