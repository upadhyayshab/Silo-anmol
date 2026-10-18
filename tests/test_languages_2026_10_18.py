"""The 12 product languages (2026-10-18): every farmer-facing string the
engine builds exists in each of them, English output is unchanged, and
report views default to the language the wizard was run in."""
import string
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

PRODUCT_LANGUAGES = {"en", "hi", "kn", "mr", "ta", "te", "ml", "gu", "bn", "or", "as", "pa"}


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


class TestSupportedLanguages(TestCase):
    def test_exactly_the_product_languages(self):
        from utils.languages import SUPPORTED_LANGUAGES, normalize_lang

        self.assertEqual(set(SUPPORTED_LANGUAGES) - {"hn"}, PRODUCT_LANGUAGES)
        self.assertEqual(normalize_lang("hn"), "hi")
        self.assertEqual(normalize_lang("as-IN"), "as")
        self.assertEqual(normalize_lang("ur"), "en")  # not a product language


class TestFarmerTextCoverage(TestCase):
    def test_every_language_has_every_string_with_the_same_placeholders(self):
        from utils import farmerText as T

        self.assertEqual(set(T.NOTES), PRODUCT_LANGUAGES)
        self.assertEqual(set(T.REASONS), PRODUCT_LANGUAGES)
        self.assertEqual(set(T.SEASON_NAMES), PRODUCT_LANGUAGES)
        self.assertEqual(set(T.SUITABILITY), PRODUCT_LANGUAGES)
        self.assertEqual(set(T.SOIL_NAMES), PRODUCT_LANGUAGES)
        self.assertEqual(set(T.PREVIOUS_CROP_FALLBACK), PRODUCT_LANGUAGES)
        for lang in PRODUCT_LANGUAGES:
            for key, english in T.NOTES["en"].items():
                # Translations may drop a value (the raw soil class number), never add one.
                self.assertLessEqual(_placeholders(T.NOTES[lang][key]), _placeholders(english), (lang, key))
            for factor, by_points in T.REASONS["en"].items():
                self.assertEqual(set(T.REASONS[lang][factor]), set(by_points), (lang, factor))
                for points, english in by_points.items():
                    self.assertEqual(
                        _placeholders(T.REASONS[lang][factor][points]), _placeholders(english), (lang, factor, points),
                    )
            self.assertEqual(set(T.SEASON_NAMES[lang]), set(T.SEASON_NAMES["en"]), lang)
            self.assertEqual(set(T.SOIL_NAMES[lang]), set(range(13)), lang)
            self.assertEqual(_placeholders(T.SUITABILITY[lang]), {"pct", "season"}, lang)

    def test_english_text_unchanged(self):
        from utils.farmerText import note, soil_name, suitability_reason
        from utils.scoringEngine import SOIL_UNAVAILABLE_NOTE, ZONE_SKIPPED_NOTE

        self.assertEqual(SOIL_UNAVAILABLE_NOTE, "Soil data is unavailable right now. Soil factor skipped.")
        self.assertEqual(ZONE_SKIPPED_NOTE, "Zone hard-filter skipped: no coordinates provided.")
        self.assertEqual(
            note("soil_detected", label="Loam", cls=9), "Soil texture auto-detected: Loam (Class 9) from coordinates.",
        )
        self.assertEqual(
            note("zone_resolved", zone=7, pincode="411001"), "Agro-climatic zone resolved to Zone 7 (from pincode 411001).",
        )
        self.assertEqual(soil_name(9), "Loam")
        self.assertEqual(suitability_reason(90.4, "RABI"), "Suitability 90% for Rabi season.")

    def test_other_languages_and_fallbacks(self):
        from utils.farmerText import note, soil_name, suitability_reason

        self.assertEqual(note("rotation_unknown", "as"), "আগৰ শস্য চিনাক্ত কৰিব পৰা নগ'ল, সেয়েহে শস্য চক্ৰ বিবেচনা কৰা হোৱা নাই।")
        self.assertEqual(suitability_reason(72, "KHARIF", "pa"), "ਸਾਉਣੀ ਮੌਸਮ ਲਈ 72% ਢੁਕਵਾਂਪਣ।")
        self.assertEqual(soil_name(10, "or"), "ବାଲି")
        self.assertEqual(note("zone_skipped", "xx"), note("zone_skipped"))  # unknown language -> English


class TestMatchReasons(TestCase):
    FACTORS = [
        {"factor": "SEASON", "points": 2}, {"factor": "BUDGET", "points": 0}, {"factor": "ROTATION", "points": 2},
    ]

    def test_english_unchanged(self):
        from utils.matchReasons import match_reasons

        self.assertEqual(
            match_reasons(self.FACTORS, "ZAID", "Wheat"),
            ["Ideal for Summer", "Good rotation after Wheat", "Above your budget"],
        )
        self.assertEqual(
            match_reasons([{"factor": "ROTATION", "points": 2}], "RABI"), ["Good rotation after your previous crop"],
        )

    def test_tamil_and_odia(self):
        from utils.matchReasons import match_reasons

        self.assertEqual(
            match_reasons(self.FACTORS, "ZAID", "கோதுமை", lang="ta"),
            ["கோடை பருவத்திற்கு மிகவும் ஏற்றது", "கோதுமை பிறகு நல்ல பயிர் சுழற்சி", "உங்கள் பட்ஜெட்டை விட அதிகம்"],
        )
        self.assertEqual(
            match_reasons([{"factor": "ROTATION", "points": 2}], "RABI", lang="or"), ["ଆପଣଙ୍କ ପୂର୍ବ ଫସଲ ପରେ ଭଲ ଫସଲ ଚକ୍ର"],
        )


class TestWizardLabels(TestCase):
    def test_every_question_and_option_in_every_language(self):
        from routers.v2.wizard import ALL_QUESTIONS, _localized

        for lang in PRODUCT_LANGUAGES - {"en"}:
            localized = _localized(ALL_QUESTIONS, lang)
            for english, translated in zip(ALL_QUESTIONS, localized):
                self.assertNotEqual(translated.label, english.label, (lang, english.id))
                self.assertEqual(translated.id, english.id)
                if english.id == "budget":
                    continue  # rupee ranges stay as they are
                for eng_opt, opt in zip(english.options or [], translated.options or []):
                    self.assertEqual(opt.value, eng_opt.value)
                    self.assertNotEqual(opt.label, eng_opt.label, (lang, english.id, eng_opt.value))


class TestSoilNoteLanguage(IsolatedAsyncioTestCase):
    async def test_detected_soil_note_in_kannada(self):
        from utils.scoringEngine import CropScoringEngine
        import utils.scoringEngine as engine_module

        lookup = MagicMock()
        lookup.get_texture = MagicMock(return_value=SimpleNamespace(status="ok", soil_class=9, message=""))
        notes: list[str] = []
        with patch.object(engine_module, "get_soil_texture_lookup", return_value=lookup):
            soil_class = await CropScoringEngine._detect_soil_class(MagicMock(), 12.9, 77.6, notes, "kn")
        self.assertEqual(soil_class, 9)
        self.assertEqual(notes, ["ನಿಮ್ಮ ಸ್ಥಳದಿಂದ ಮಣ್ಣಿನ ಪ್ರಕಾರ ಗುರುತಿಸಲಾಗಿದೆ: ಗೋಡು ಮಣ್ಣು."])


def _manager(rows):
    manager = MagicMock()
    manager.fetch_all = AsyncMock(return_value=MagicMock(items=rows, count=len(rows)))
    return manager


def _report_row(crop_id, name_en, name_local):
    return SimpleNamespace(
        crop_id=crop_id, name_en=name_en, name_local=name_local, family="Oilseed",
        crop_type="annual", seasons=["rabi"], duration_days=110, years_to_first_harvest=None,
        cost_per_acre_inr=12000, expected_yield_per_acre=8.0, yield_unit="quintal",
        profit_potential="high", sowing_windows=[], profit_note="note", weather=[], risks=[], tips=[],
        soil_economic_benefits={"title": "t", "soil": [], "economic": []},
        hero_image_url=None, icon_url=None, water_requirement="Low",
    )


class TestReportDefaultsToRunLanguage(IsolatedAsyncioTestCase):
    def _service(self):
        from services.cropReportService import CropReportService

        run = SimpleNamespace(
            uid="ucr-1", recommendation_id="rec-1", user_id="usr_1", farm_id="farms_1", farm_name="East Plot",
            season="RABI", top_crop_name="Mustard", top_crop_score=90, total_recommendations=1, language="mr",
            created_at=datetime(2026, 10, 18, 9, 30, tzinfo=timezone.utc),
            report_data={
                "results": [{"crop": "Mustard", "suitability_pct": 90.0, "factors": [{"factor": "ROTATION", "points": 2}]}],
                "previous_crop": "Wheat",
            },
        )
        rows = [
            _report_row("mustard", "Mustard", {"hi": "सरसों", "mr": "मोहरी"}),
            _report_row("wheat", "Wheat", {"hi": "गेहूं", "mr": "गहू"}),
        ]
        return CropReportService(crop_report_manager=_manager(rows), user_crop_report_manager=_manager([run]))

    async def test_unified_report_without_lang_uses_run_language(self):
        report = await self._service().get_or_create_unified_report("rec-1", "usr_1")
        self.assertEqual(report.language, "mr")
        self.assertEqual(report.crops[0].match_reasons, ["गहू नंतर चांगली फेरपालट"])

    async def test_explicit_lang_wins(self):
        report = await self._service().get_or_create_unified_report("rec-1", "usr_1", lang="en")
        self.assertEqual(report.language, "en")
        self.assertEqual(report.crops[0].match_reasons, ["Good rotation after Wheat"])

    async def test_list_without_lang_uses_each_run_language(self):
        listing = await self._service().list_user_reports("usr_1")
        self.assertEqual(listing.reports[0].top_crop_vernacular, "मोहरी")
