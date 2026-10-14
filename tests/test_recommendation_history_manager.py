"""Regression test for review finding Critical #1: RecommendationHistorySchema.
sowing_date is a real db.Date() column, and SQLAlchemy does NOT coerce a
Python str into a date for a Date column -- passing a raw ISO string (as the
wizard router used to) raises a TypeError against a real engine. The unit
tests in test_wizard_submit.py mock history_manager.create entirely, so they
can't catch this; this test exercises the REAL RecommendationHistoryManager
against a real (in-memory sqlite) engine to prove the write actually
round-trips a real date object without raising.

Engine/table-creation pattern follows app.py's own lifespan
(`BaseSchema.metadata.create_all`) and config.py's Settings.engine_str, which
already defaults to sqlite+aiosqlite:///:memory: when DB_HOST isn't set.
"""
import sys
from datetime import date
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from sqlalchemy.ext.asyncio import create_async_engine

from SharedBackend.managers import BaseSchema
from managers.cropManagers import RecommendationHistoryManager, RecommendationHistorySchema


class TestRecommendationHistoryManagerRealEngine(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(BaseSchema.metadata.create_all)
        self.history_manager = RecommendationHistoryManager(self.engine)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_sowing_date_round_trips_as_real_date_object(self):
        created = await self.history_manager.create(RecommendationHistorySchema(
            upid="usr_1",
            subscription_id="sub_1",
            lat=18.52,
            lon=73.85,
            season="RABI",
            water_methods=["DRIP"],
            budget_bucket="B3",
            previous_crop_code=None,
            was_fallow=True,
            top_n=5,
            lang="en",
            zone_filter_applied=False,
            soil_class_detected=None,
            soil_texture_label=None,
            results=[],
            notes=[],
            farm_id=None,
            farm_name=None,
            livestock=None,
            current_crop_code=None,
            sowing_date=date(2026, 10, 15),
        ))

        self.assertIsInstance(created.sowing_date, date)
        self.assertEqual(created.sowing_date, date(2026, 10, 15))

        # Re-fetch independently to prove it actually persisted, not just that
        # the in-memory object echoed back what was passed in.
        fetched = await self.history_manager.fetch_all(limit=1, filters={"upid": "usr_1"})
        self.assertEqual(len(fetched.items), 1)
        self.assertEqual(fetched.items[0].sowing_date, date(2026, 10, 15))

    async def test_sowing_date_none_round_trips(self):
        created = await self.history_manager.create(RecommendationHistorySchema(
            upid="usr_2",
            subscription_id="sub_1",
            lat=None,
            lon=None,
            season="KHARIF",
            water_methods=["RAINFED"],
            budget_bucket="B1",
            previous_crop_code=None,
            was_fallow=True,
            top_n=5,
            lang="en",
            zone_filter_applied=False,
            soil_class_detected=None,
            soil_texture_label=None,
            results=[],
            notes=[],
            farm_id=None,
            farm_name=None,
            livestock=None,
            current_crop_code=None,
            sowing_date=None,
        ))

        self.assertIsNone(created.sowing_date)
