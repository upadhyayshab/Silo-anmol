import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


def _crop_report_row(crop_id="mustard", name_en="Mustard", family="Brassica"):
    return MagicMock(
        crop_id=crop_id, name_en=name_en, name_local={"hi": "सरसों"}, family=family,
        crop_type="annual", seasons=["rabi"], duration_days=115, years_to_first_harvest=None,
        cost_per_acre_inr=12000, expected_yield_per_acre=8.0, yield_unit="quintal",
        profit_potential="high", sowing_windows=[{"season": "rabi", "start": "10-01", "end": "10-20"}],
        profit_note="note", weather=[], risks=[], tips=[],
        soil_economic_benefits={"title": "t", "soil": [], "economic": []},
        hero_image_url=None, icon_url=None,
    )


class TestGetMasterReport(IsolatedAsyncioTestCase):
    async def test_returns_dossier_for_known_crop(self):
        from services.cropReportService import CropReportService

        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(
            return_value=MagicMock(items=[_crop_report_row()])
        )
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=MagicMock(),
        )

        dossier = await service.get_master_report("mustard", lang="en")

        self.assertEqual(dossier.crop_id, "mustard")
        self.assertEqual(dossier.name_en, "Mustard")
        self.assertEqual(dossier.family, "Brassica")


class TestGetOrCreateUnifiedReport(IsolatedAsyncioTestCase):
    async def test_builds_dossier_per_ranked_crop_from_cached_run(self):
        from services.cropReportService import CropReportService

        mock_user_crop_report_manager = MagicMock()
        mock_user_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            MagicMock(
                uid="ucr-1", recommendation_id="rec-1", user_id="usr_1", farm_id=None,
                farm_name="East Plot", season="RABI", top_crop_name="Mustard", top_crop_score=95,
                report_data={"results": [
                    {"crop": "Mustard", "family": "Brassica", "suitability_pct": 95.0},
                    {"crop": "Wheat", "family": "Cereal", "suitability_pct": 84.0},
                ]},
            )
        ]))
        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(side_effect=[
            MagicMock(items=[_crop_report_row(crop_id="mustard", name_en="Mustard", family="Brassica")]),
            MagicMock(items=[_crop_report_row(crop_id="wheat", name_en="Wheat", family="Cereal")]),
        ])
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=mock_user_crop_report_manager,
        )

        report = await service.get_or_create_unified_report(recommendation_id="rec-1", user_id="usr_1")

        self.assertEqual(report.recommendation_id, "rec-1")
        self.assertEqual(report.total_recommended_crops, 2)
        self.assertEqual(len(report.crops), 2)
        self.assertTrue(report.crops[0].is_top_pick)
        self.assertFalse(report.crops[1].is_top_pick)

    async def test_crop_missing_from_master_catalog_is_skipped_not_500(self):
        # Review Focus: a scored crop with no crop_reports row must be
        # omitted from the dossier list, not crash the whole report.
        from services.cropReportService import CropReportService

        mock_user_crop_report_manager = MagicMock()
        mock_user_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            MagicMock(
                uid="ucr-1", recommendation_id="rec-2", user_id="usr_1", farm_id=None,
                farm_name=None, season="RABI", top_crop_name="ObscureCrop", top_crop_score=70,
                report_data={"results": [{"crop": "ObscureCrop", "family": "Unknown", "suitability_pct": 70.0}]},
            )
        ]))
        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))  # no master row
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=mock_user_crop_report_manager,
        )

        report = await service.get_or_create_unified_report(recommendation_id="rec-2", user_id="usr_1")

        self.assertEqual(report.crops, [])
        self.assertEqual(report.total_recommended_crops, 1)  # count reflects the scoring run, not the dossiers found
