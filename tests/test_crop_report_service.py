import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


def _crop_report_row(crop_id="mustard", name_en="Mustard", family="Brassica",
                      name_local=None, hero_image_url=None, icon_url=None):
    return MagicMock(
        crop_id=crop_id, name_en=name_en, name_local=name_local or {"hi": "सरसों"}, family=family,
        crop_type="annual", seasons=["rabi"], duration_days=115, years_to_first_harvest=None,
        cost_per_acre_inr=12000, expected_yield_per_acre=8.0, yield_unit="quintal",
        profit_potential="high", sowing_windows=[{"season": "rabi", "start": "10-01", "end": "10-20"}],
        profit_note="note", weather=[], risks=[], tips=[],
        soil_economic_benefits={"title": "t", "soil": [], "economic": []},
        hero_image_url=hero_image_url, icon_url=icon_url,
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


    async def test_resolves_any_case_spelling_or_local_name(self):
        from services.cropReportService import CropReportService

        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            _crop_report_row(),
            _crop_report_row(crop_id="lime_lemon", name_en="Lime / Lemon", name_local={"hi": "नींबू"}),
        ]))
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=MagicMock(),
        )

        for query, expected in [("MUSTARD", "mustard"), ("सरसों", "mustard"), ("Lime/lemon", "lime_lemon")]:
            self.assertEqual((await service.get_master_report(query)).crop_id, expected)
        mock_crop_report_manager.fetch_all.assert_awaited_once_with(limit=0)


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
        # The catalog loads every crop_reports row once, then resolves in memory.
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            _crop_report_row(crop_id="mustard", name_en="Mustard", family="Brassica"),
            _crop_report_row(crop_id="wheat", name_en="Wheat", family="Cereal"),
        ]))
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

    async def test_actual_top_pick_missing_from_catalog_rebase_onto_survivor(self):
        # Review finding: when the ranked-index-0 crop (the real top pick) has no
        # crop_reports row, the surviving dossier (ranked index 1) must become the
        # is_top_pick=True entry, and top_crop_id/top_crop_name/top_crop_score must
        # all consistently describe that SAME surviving crop — not three different
        # crops (one from top_crop_id, another from run.top_crop_name/score, and
        # none flagged is_top_pick in `crops`).
        from services.cropReportService import CropReportService

        mock_user_crop_report_manager = MagicMock()
        mock_user_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            MagicMock(
                uid="ucr-3", recommendation_id="rec-3", user_id="usr_1", farm_id=None,
                farm_name="North Plot", season="RABI", top_crop_name="ObscureCrop", top_crop_score=95,
                report_data={"results": [
                    {"crop": "ObscureCrop", "family": "Unknown", "suitability_pct": 95.0},
                    {"crop": "Wheat", "family": "Cereal", "suitability_pct": 84.0},
                ]},
            )
        ]))
        mock_crop_report_manager = MagicMock()
        # ObscureCrop has no catalog row; Wheat does.
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            _crop_report_row(crop_id="wheat", name_en="Wheat", family="Cereal"),
        ]))
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=mock_user_crop_report_manager,
        )

        report = await service.get_or_create_unified_report(recommendation_id="rec-3", user_id="usr_1")

        self.assertEqual(len(report.crops), 1)
        self.assertEqual(report.crops[0].crop_id, "wheat")
        self.assertTrue(report.crops[0].is_top_pick)
        self.assertEqual(report.top_crop_id, "wheat")
        self.assertEqual(report.top_crop_name, "Wheat")
        self.assertEqual(report.top_crop_score, 84)


class TestMoreForYourFarm(IsolatedAsyncioTestCase):
    async def test_more_for_your_farm_populated_from_dossiers_after_top_pick(self):
        # GAP-09: more_for_your_farm was hardcoded to []; it must now carry
        # every surviving dossier ranked after the top pick.
        from services.cropReportService import CropReportService

        mock_user_crop_report_manager = MagicMock()
        mock_user_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            MagicMock(
                uid="ucr-4", recommendation_id="rec-4", user_id="usr_1", farm_id=None,
                farm_name="East Plot", season="RABI", top_crop_name="Mustard", top_crop_score=95,
                report_data={"results": [
                    {"crop": "Mustard", "family": "Brassica", "suitability_pct": 95.0},
                    {"crop": "Wheat", "family": "Cereal", "suitability_pct": 84.0},
                    {"crop": "Gram", "family": "Legume", "suitability_pct": 70.0},
                ]},
            )
        ]))
        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            _crop_report_row(crop_id="mustard", name_en="Mustard", family="Brassica"),
            _crop_report_row(crop_id="wheat", name_en="Wheat", family="Cereal"),
            _crop_report_row(crop_id="gram", name_en="Gram", family="Legume"),
        ]))
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=mock_user_crop_report_manager,
        )

        report = await service.get_or_create_unified_report(recommendation_id="rec-4", user_id="usr_1")

        self.assertEqual(len(report.more_for_your_farm), 2)
        self.assertEqual([i.crop_id for i in report.more_for_your_farm], ["wheat", "gram"])
        self.assertTrue(all(not i.is_top_pick for i in report.more_for_your_farm))


class TestListUserReportsLocalizationAndThumbnail(IsolatedAsyncioTestCase):
    async def test_crop_name_vernacular_resolved_from_master_row_name_local(self):
        # GAP-08: crop_name_vernacular/top_crop_vernacular were always the
        # English name; they must resolve through master_row.name_local[lang].
        from services.cropReportService import CropReportService

        mock_user_crop_report_manager = MagicMock()
        mock_user_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            MagicMock(
                uid="ucr-5", recommendation_id="rec-5", user_id="usr_1", farm_id=None,
                farm_name=None, season="RABI", top_crop_name="Mustard", top_crop_score=95,
                total_recommendations=1, created_at=None,
                report_data={"results": [{"crop": "Mustard", "family": "Brassica", "suitability_pct": 95.0}]},
            )
        ]))
        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(
            items=[_crop_report_row(name_local={"hi": "सरसों / राय"})]
        ))
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=mock_user_crop_report_manager,
        )

        result = await service.list_user_reports(user_id="usr_1", lang="hi")

        card = result.reports[0]
        self.assertEqual(card.top_crop_vernacular, "सरसों / राय")
        self.assertEqual(card.recommended_crops_summary[0].crop_name_vernacular, "सरसों / राय")

    async def test_vernacular_falls_back_to_english_when_lang_missing_from_name_local(self):
        from services.cropReportService import CropReportService

        mock_user_crop_report_manager = MagicMock()
        mock_user_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            MagicMock(
                uid="ucr-6", recommendation_id="rec-6", user_id="usr_1", farm_id=None,
                farm_name=None, season="RABI", top_crop_name="Mustard", top_crop_score=95,
                total_recommendations=1, created_at=None,
                report_data={"results": [{"crop": "Mustard", "family": "Brassica", "suitability_pct": 95.0}]},
            )
        ]))
        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(
            items=[_crop_report_row(name_local={"hi": "सरसों / राय"})]
        ))
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=mock_user_crop_report_manager,
        )

        result = await service.list_user_reports(user_id="usr_1", lang="mr")  # not in name_local

        self.assertEqual(result.reports[0].top_crop_vernacular, "Mustard")

    async def test_thumbnail_url_populated_from_top_crop_icon_url(self):
        # GAP-10: thumbnail_url was always omitted; it must resolve from the
        # top crop's icon_url (falling back to hero_image_url).
        from services.cropReportService import CropReportService

        mock_user_crop_report_manager = MagicMock()
        mock_user_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            MagicMock(
                uid="ucr-7", recommendation_id="rec-7", user_id="usr_1", farm_id=None,
                farm_name=None, season="RABI", top_crop_name="Mustard", top_crop_score=95,
                total_recommendations=1, created_at=None,
                report_data={"results": [{"crop": "Mustard", "family": "Brassica", "suitability_pct": 95.0}]},
            )
        ]))
        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(
            items=[_crop_report_row(icon_url="https://cdn.example/mustard-icon.png")]
        ))
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=mock_user_crop_report_manager,
        )

        result = await service.list_user_reports(user_id="usr_1")

        self.assertEqual(result.reports[0].thumbnail_url, "https://cdn.example/mustard-icon.png")


class TestListUserReportsCropIdResolution(IsolatedAsyncioTestCase):
    """Regression for review finding Important #3: the real crop_reports.crop_id
    doesn't always follow the naive `name.lower().replace(" ", "_")` slug rule
    (e.g. a crop named 'Rice (Paddy)' has a real crop_id that is NOT
    'rice_(paddy)'). list_user_reports must resolve the REAL catalog crop_id
    through the same _find_master_row lookup get_or_create_unified_report
    already uses, not fabricate a different id via naive slugification."""

    async def test_crop_id_resolved_from_catalog_not_naive_slug(self):
        from services.cropReportService import CropReportService

        mock_user_crop_report_manager = MagicMock()
        mock_user_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            MagicMock(
                uid="ucr-9", recommendation_id="rec-9", user_id="usr_1", farm_id=None,
                farm_name=None, season="RABI", top_crop_name="Rice (Paddy)", top_crop_score=88,
                total_recommendations=1, created_at=None,
                report_data={"results": [
                    {"crop": "Rice (Paddy)", "family": "Cereal", "suitability_pct": 88.0},
                ]},
            )
        ]))
        mock_crop_report_manager = MagicMock()
        # Real catalog row's crop_id does NOT match the naive slug
        # ("rice_(paddy)") -- this is the whole point of the regression.
        mock_crop_report_manager.fetch_all = AsyncMock(
            return_value=MagicMock(items=[_crop_report_row(crop_id="paddy_rice", name_en="Rice (Paddy)")])
        )
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=mock_user_crop_report_manager,
        )

        result = await service.list_user_reports(user_id="usr_1")

        card = result.reports[0]
        self.assertEqual(card.top_crop_id, "paddy_rice")
        self.assertEqual(card.recommended_crops_summary[0].crop_id, "paddy_rice")

    async def test_crop_id_falls_back_to_naive_slug_when_not_in_catalog(self):
        from services.cropReportService import CropReportService

        mock_user_crop_report_manager = MagicMock()
        mock_user_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[
            MagicMock(
                uid="ucr-10", recommendation_id="rec-10", user_id="usr_1", farm_id=None,
                farm_name=None, season="RABI", top_crop_name="Obscure Crop", top_crop_score=70,
                total_recommendations=1, created_at=None,
                report_data={"results": [
                    {"crop": "Obscure Crop", "family": "Unknown", "suitability_pct": 70.0},
                ]},
            )
        ]))
        mock_crop_report_manager = MagicMock()
        mock_crop_report_manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))  # no catalog row at all
        service = CropReportService(
            crop_report_manager=mock_crop_report_manager, user_crop_report_manager=mock_user_crop_report_manager,
        )

        result = await service.list_user_reports(user_id="usr_1")

        card = result.reports[0]
        self.assertEqual(card.top_crop_id, "obscure_crop")
        self.assertEqual(card.recommended_crops_summary[0].crop_id, "obscure_crop")
