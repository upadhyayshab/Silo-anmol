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
        self.assertEqual(by_id["previous_crop"].depends_on, {"field": "has_active_crop", "value": "No"})
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
