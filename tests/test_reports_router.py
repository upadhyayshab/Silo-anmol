import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestReportsRouter(IsolatedAsyncioTestCase):
    @patch("routers.v2.reports.report_service")
    async def test_list_user_reports_delegates_to_service(self, mock_service):
        from routers.v2.reports import list_user_reports
        from models.reportModels import UserReportsListResponse
        mock_service.list_user_reports = AsyncMock(
            return_value=UserReportsListResponse(user_id="usr_1", total=0, reports=[])
        )

        result = await list_user_reports(
            user_id="usr_1", farm_id=None, recommendation_id=None, lang="en",
            auth_payload={"upid": "usr_1"},
        )

        self.assertEqual(result.user_id, "usr_1")
        mock_service.list_user_reports.assert_awaited_once_with(
            user_id="usr_1", farm_id=None, recommendation_id=None, lang="en",
        )

    @patch("routers.v2.reports.report_service")
    async def test_get_master_crop_report_lowercases_crop_id(self, mock_service):
        from routers.v2.reports import get_master_crop_report
        from models.reportModels import SingleCropDossier, SoilEconomicBenefits
        mock_service.get_master_report = AsyncMock(return_value=SingleCropDossier(
            crop_id="mustard", name_en="Mustard", name_local={}, match_score=0, is_top_pick=False,
            match_reasons=[], family="Brassica", crop_type="annual", seasons=["rabi"],
            cost_per_acre_inr=12000, expected_yield_per_acre=8.0, yield_unit="quintal",
            profit_potential="high", profit_note="n", sowing_windows=[], weather=[], risks=[], tips=[],
            soil_economic_benefits=SoilEconomicBenefits(soil=[], economic=[]),
        ))

        await get_master_crop_report(crop_id="MUSTARD", lang="en", auth_payload={"upid": "usr_1"})

        mock_service.get_master_report.assert_awaited_once_with(crop_id="mustard", lang="en")
