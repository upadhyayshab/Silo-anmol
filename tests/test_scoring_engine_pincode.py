"""TRD §Phase 4: a farmer without GPS can submit a pincode directly and
still get zone-filtered recommendations."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestScoringEnginePincodeFallback(IsolatedAsyncioTestCase):
    async def _make_engine_with_mocks(self):
        from utils.scoringEngine import CropScoringEngine
        engine = CropScoringEngine.__new__(CropScoringEngine)  # bypass __init__'s real managers
        engine.crop_manager = MagicMock()
        engine.crop_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))
        engine.season_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.water_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.soil_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.budget_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.rotation_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.weight_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[
            MagicMock(factor="SEASON", weight=1), MagicMock(factor="WATER", weight=1),
            MagicMock(factor="ZONE", weight=1), MagicMock(factor="SOIL", weight=1),
            MagicMock(factor="BUDGET", weight=1), MagicMock(factor="ROTATION", weight=1),
        ])))
        engine.zone_manager = MagicMock(fetch_all=AsyncMock(return_value=MagicMock(items=[])))
        engine.pincode_manager = MagicMock()
        engine.location_service = MagicMock()
        return engine

    async def test_direct_pincode_resolves_zone_without_coordinates(self):
        from models.recommendationModels import RecommendationRequest
        engine = await self._make_engine_with_mocks()
        engine.pincode_manager.fetch_all = AsyncMock(
            return_value=MagicMock(items=[MagicMock(zone=9)])
        )

        request = RecommendationRequest(
            lat=None, lon=None, pincode="411001", season="RABI", water_methods=["DRIP"],
            budget_bucket="B1", was_fallow=True,
        )
        response = await engine.recommend(request)

        self.assertTrue(response.zone_filter_applied)
        engine.pincode_manager.fetch_all.assert_awaited_once_with(limit=1, filters={"pincode": "411001"})
        engine.location_service.get_pincode_from_coordinates.assert_not_called()

    async def test_unmapped_pincode_skips_zone_filter_not_raise(self):
        from models.recommendationModels import RecommendationRequest
        engine = await self._make_engine_with_mocks()
        engine.pincode_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))

        request = RecommendationRequest(
            lat=None, lon=None, pincode="000000", season="RABI", water_methods=["DRIP"],
            budget_bucket="B1", was_fallow=True,
        )
        response = await engine.recommend(request)  # must NOT raise

        self.assertFalse(response.zone_filter_applied)

    async def test_coordinates_still_take_priority_over_pincode(self):
        from models.recommendationModels import RecommendationRequest
        engine = await self._make_engine_with_mocks()
        engine.location_service.get_pincode_from_coordinates = AsyncMock(return_value="411001")
        engine.pincode_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[MagicMock(zone=9)]))

        request = RecommendationRequest(
            lat=18.5, lon=73.8, pincode="999999", season="RABI", water_methods=["DRIP"],
            budget_bucket="B1", was_fallow=True,
        )
        await engine.recommend(request)

        engine.location_service.get_pincode_from_coordinates.assert_awaited_once_with(18.5, 73.8)
