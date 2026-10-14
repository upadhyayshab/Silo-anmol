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
