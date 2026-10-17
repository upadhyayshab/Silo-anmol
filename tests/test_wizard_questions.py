"""Unit tests for GET /wizard/questions — suppression logic per TRD §8.2."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestWizardQuestions(IsolatedAsyncioTestCase):
    @patch("routers.v2.wizard.crop_manager")
    async def test_no_farm_id_returns_all_questions_unsuppressed(self, mock_crop_manager):
        from routers.v2.wizard import get_wizard_questions

        mock_crops_result = MagicMock()
        mock_crops_result.items = [MagicMock(name_en="Rice"), MagicMock(name_en="Wheat")]
        mock_crop_manager.fetch_all = AsyncMock(return_value=mock_crops_result)

        result = await get_wizard_questions(farmId=None, auth_payload={"upid": "usr_1"})

        self.assertIsNone(result.farm_context)
        question_ids = [q.id for q in result.questions]
        self.assertIn("location", question_ids)
        self.assertIn("season", question_ids)
        self.assertIn("has_active_crop", question_ids)
        self.assertIn("irrigation", question_ids)
        self.assertIn("budget", question_ids)
        self.assertIn("livestock", question_ids)
        self.assertIn("farm_name", question_ids)

        by_id = {q.id: q for q in result.questions}
        # GAP-01: SDUI branching metadata
        self.assertEqual(by_id["crop"].depends_on, {"field": "has_active_crop", "value": "Yes"})
        self.assertEqual(by_id["sowing_date"].depends_on, {"field": "has_active_crop", "value": "Yes"})
        # Crop Guide flowchart asks "Which crop did you grow before?" on both
        # branches, so previous_crop no longer depends on has_active_crop.
        self.assertIsNone(by_id["previous_crop"].depends_on)
        # GAP-02: autocomplete endpoint
        self.assertEqual(by_id["crop"].search_endpoint, "/api/v6/crops")
        self.assertEqual(by_id["previous_crop"].search_endpoint, "/api/v6/crops")
        # GAP-03: 4 canonical budget brackets (not the old 3-tier low/medium/high)
        budget_values = [o.value for o in by_id["budget"].options]
        self.assertEqual(budget_values, ["<₹10k", "₹10–25k", "₹25–50k", ">₹50k"])
        # GAP-04: all 5 flowchart/TRD livestock categories
        livestock_values = [o.value for o in by_id["livestock"].options]
        self.assertEqual(len(livestock_values), 5)
        self.assertIn("Poultry farm", livestock_values)
        self.assertIn("Fisheries", livestock_values)

    @patch("routers.v2.wizard.agro_internal_service")
    @patch("routers.v2.wizard.crop_manager")
    async def test_farm_id_with_center_suppresses_location_and_crop_fields(
        self, mock_crop_manager, mock_agro_service
    ):
        from routers.v2.wizard import get_wizard_questions

        mock_crops_result = MagicMock()
        mock_crops_result.items = []
        mock_crop_manager.fetch_all = AsyncMock(return_value=mock_crops_result)

        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-1", "name": "Green Valley Farm", "center": [77.7, 13.1],
            "crop": "Tomato", "has_active_crop": True, "previous_crop": "Wheat",
            "irrigation_methods": ["Drip"], "boundary_status": "MAPPED",
        })

        result = await get_wizard_questions(farmId="farm-1", auth_payload={"upid": "usr_1"})

        self.assertIsNotNone(result.farm_context)
        self.assertEqual(result.farm_context.farm_id, "farm-1")
        question_ids = [q.id for q in result.questions]
        self.assertNotIn("location", question_ids)
        self.assertNotIn("farm_name", question_ids)
        self.assertNotIn("has_active_crop", question_ids)
        self.assertNotIn("crop", question_ids)
        self.assertNotIn("sowing_date", question_ids)
        self.assertNotIn("previous_crop", question_ids)
        self.assertNotIn("irrigation", question_ids)
        # season, budget, livestock are never farm-derived — always present
        self.assertIn("season", question_ids)
        self.assertIn("budget", question_ids)
        self.assertIn("livestock", question_ids)
        # Regression for review finding Critical #2: the authenticated upid
        # must actually be forwarded to Agro's ownership-scoped lookup, not
        # an empty string (which would make the internal endpoint reject it).
        mock_agro_service.get_farm.assert_awaited_once_with("farm-1", "usr_1")

    @patch("routers.v2.wizard.agro_internal_service")
    @patch("routers.v2.wizard.crop_manager")
    async def test_active_crop_suppresses_previous_crop_even_when_farm_has_none_recorded(
        self, mock_crop_manager, mock_agro_service
    ):
        # GAP-05 regression: a standing active crop makes previous_crop moot
        # even when the farm record's own previous_crop field is null/absent
        # (SUPPRESSION_RULES alone only fires on a truthy previous_crop).
        from routers.v2.wizard import get_wizard_questions

        mock_crops_result = MagicMock()
        mock_crops_result.items = []
        mock_crop_manager.fetch_all = AsyncMock(return_value=mock_crops_result)

        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-2", "name": "East Plot", "center": [77.7, 13.1],
            "crop": "Tomato", "has_active_crop": True, "previous_crop": None,
        })

        result = await get_wizard_questions(farmId="farm-2", auth_payload={"upid": "usr_1"})

        question_ids = [q.id for q in result.questions]
        self.assertNotIn("previous_crop", question_ids)

    @patch("routers.v2.wizard.agro_internal_service")
    @patch("routers.v2.wizard.crop_manager")
    async def test_farm_id_agro_unreachable_degrades_to_unsuppressed(
        self, mock_crop_manager, mock_agro_service
    ):
        # Review Focus: Agro being down must not crash /wizard/questions.
        from routers.v2.wizard import get_wizard_questions

        mock_crops_result = MagicMock()
        mock_crops_result.items = []
        mock_crop_manager.fetch_all = AsyncMock(return_value=mock_crops_result)
        mock_agro_service.get_farm = AsyncMock(return_value=None)

        result = await get_wizard_questions(farmId="farm-unreachable", auth_payload={"upid": "usr_1"})

        self.assertIsNone(result.farm_context)
        question_ids = [q.id for q in result.questions]
        self.assertIn("location", question_ids)  # nothing suppressed — degrade gracefully

    @patch("routers.v2.wizard.agro_internal_service")
    async def test_selected_farm_without_crop_asks_only_season_budget_livestock(self, mock_agro_service):
        # Crop Guide flowchart, Has Farm Path: the farm answers the crop
        # questions even when it has no crop recorded (it used to show 7).
        from routers.v2.wizard import get_wizard_questions

        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-3", "name": "North Field", "center": [77.7, 13.1],
            "crop": None, "has_active_crop": False, "previous_crop": None, "irrigation_methods": ["Drip"],
        })

        result = await get_wizard_questions(farmId="farm-3", auth_payload={"upid": "usr_1"})

        self.assertEqual([q.id for q in result.questions], ["season", "budget", "livestock"])

    @patch("routers.v2.wizard.agro_internal_service")
    async def test_selected_farm_missing_location_or_known_irrigation_still_asks_them(self, mock_agro_service):
        # Scoring can't run without a location or a watering method, so a
        # farm that lacks them (or only has a value the engine can't map)
        # still asks.
        from routers.v2.wizard import get_wizard_questions

        mock_agro_service.get_farm = AsyncMock(return_value={
            "farm_id": "farm-4", "name": "Pending Plot", "center": None,
            "crop": "Tomato", "has_active_crop": True, "irrigation_methods": ["Borewell"],
        })

        result = await get_wizard_questions(farmId="farm-4", auth_payload={"upid": "usr_1"})

        self.assertEqual([q.id for q in result.questions], ["location", "season", "irrigation", "budget", "livestock"])

    async def test_hindi_labels_with_unchanged_values_and_budget(self):
        from routers.v2.wizard import get_wizard_questions

        result = await get_wizard_questions(farmId=None, lang="hi-IN", auth_payload={"upid": "usr_1"})

        by_id = {q.id: q for q in result.questions}
        self.assertEqual(by_id["location"].label, "आपका खेत कहाँ है?")
        season = {o.value: o.label for o in by_id["season"].options}
        self.assertEqual(season["Kharif"], "खरीफ (मानसून: जून - अक्टूबर)")
        # Values stay English: the submit handler maps them.
        self.assertEqual([o.value for o in by_id["irrigation"].options], ["Rainfed", "Flood", "Drip", "Sprinkler"])
        # Budget options are unchanged in every language (decision D3).
        budget = by_id["budget"].options
        self.assertEqual([o.value for o in budget], ["<₹10k", "₹10–25k", "₹25–50k", ">₹50k"])
        self.assertEqual([o.label for o in budget], ["<₹10k", "₹10–25k", "₹25–50k", ">₹50k"])

    async def test_unsupported_language_falls_back_to_english(self):
        from routers.v2.wizard import get_wizard_questions

        result = await get_wizard_questions(farmId=None, lang="xx", auth_payload={"upid": "usr_1"})

        self.assertEqual(result.questions[0].label, "Where is your farm?")
