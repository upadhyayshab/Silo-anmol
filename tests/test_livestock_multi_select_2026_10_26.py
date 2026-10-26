"""Livestock is a multi-select question: several kinds can be picked, "I don't
have any livestock" can't be combined with them, and an older app's single
value still works. Answers travel as one text value ("; "-separated), so the
recommendation history row and the CropGuideCompleted event keep their shape."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from models.wizardModels import WizardAnswersPayload, WizardSubmitRequest

COWS = "Cattle farm - Cows & Buffaloes"
GOATS = "Cattle farm - Goat, Sheep, & Pig"
NONE = "I don't have any live stock"


class TestMapLivestock(TestCase):
    def map(self, answer):
        from routers.v2.wizard import _map_livestock
        return _map_livestock(answer)

    def test_several_kinds_join_in_question_order(self):
        self.assertEqual(self.map(["Fisheries", COWS, "Poultry farm"]), f"{COWS}; Poultry farm; Fisheries")

    def test_goat_option_keeps_its_commas(self):
        self.assertEqual(self.map([GOATS, COWS]), f"{COWS}; {GOATS}")

    def test_single_value_from_older_app_builds(self):
        self.assertEqual(self.map(COWS), COWS)
        self.assertEqual(self.map("poultry FARM"), "Poultry farm")  # case and spaces don't matter

    def test_no_livestock_alone(self):
        self.assertEqual(self.map([NONE]), NONE)
        self.assertEqual(self.map("I don't have any livestock"), NONE)  # the label's spelling works too

    def test_english_labels_are_accepted_and_stored_as_values(self):
        self.assertEqual(self.map(["Fish, Prawns, & Crab", "Cows & Buffaloes", "cock, hen, & duck"]),
                         f"{COWS}; Poultry farm; Fisheries")
        with self.assertRaises(ValueError):
            self.map(["I don't have any live stock", "Goat, Sheep, & Pig"])

    def test_no_livestock_is_exclusive(self):
        with self.assertRaises(ValueError) as raised:
            self.map([NONE, COWS])
        self.assertIn("can't be combined", str(raised.exception))

    def test_unknown_option_is_rejected(self):
        with self.assertRaises(ValueError):
            self.map([COWS, "Horses"])

    def test_not_answered(self):
        for answer in (None, [], [""], ["  "]):
            self.assertIsNone(self.map(answer), answer)

    def test_duplicates_collapse(self):
        self.assertEqual(self.map([COWS, COWS, "cattle farm - cows & buffaloes"]), COWS)

    def test_all_four_kinds_fit_the_text_columns(self):
        self.assertLessEqual(len(self.map([COWS, GOATS, "Poultry farm", "Fisheries"])), 255)


class TestQuestion(IsolatedAsyncioTestCase):
    async def test_livestock_is_multi_select_with_the_same_options(self):
        from routers.v2.wizard import ALL_QUESTIONS
        q = next(q for q in ALL_QUESTIONS if q.id == "livestock")
        self.assertEqual(q.type, "multi_select")
        self.assertEqual([o.value for o in q.options], [COWS, GOATS, "Poultry farm", "Fisheries", NONE])

    async def test_labels_and_description_follow_the_app_design(self):
        """Display text changed (2026-10-26 design); values (the contract) did not."""
        from routers.v2.wizard import ALL_QUESTIONS
        q = next(q for q in ALL_QUESTIONS if q.id == "livestock")
        self.assertEqual([o.label for o in q.options],
                         ["Cows & Buffaloes", "Goat, Sheep, & Pig", "Cock, Hen, & Duck", "Fish, Prawns, & Crab",
                          "I don't have any live stock"])
        self.assertEqual(q.description,
                         "Choose the option that best fits your farm. We'll tailor practical advice to your animals.")

    async def test_every_language_has_the_labels_and_description(self):
        from routers.v2.wizard import ALL_QUESTIONS, _localized
        en = next(q for q in ALL_QUESTIONS if q.id == "livestock")
        for lang in ("hi", "mr", "kn", "ta", "te", "ml", "gu", "bn", "or", "as", "pa"):
            q = next(q for q in _localized(ALL_QUESTIONS, lang) if q.id == "livestock")
            self.assertEqual([o.value for o in q.options], [o.value for o in en.options], lang)  # values never translated
            self.assertTrue(all(o.label != e.label for o, e in zip(q.options, en.options)), lang)  # every label translated
            self.assertTrue(q.description and q.description != en.description, lang)
        hi = next(q for q in _localized(ALL_QUESTIONS, "hi") if q.id == "livestock")
        self.assertEqual(hi.options[2].label, "मुर्गा, मुर्गी और बत्तख")


def _empty_manager():
    manager = MagicMock()
    manager.fetch_all = AsyncMock(return_value=MagicMock(items=[], count=0))
    manager.create = AsyncMock()
    return manager


class TestSubmit(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from models.recommendationModels import CropRecommendation, RecommendationResponse
        self.patches = {name: patch(f"routers.v2.wizard.{name}") for name in (
            "scoring_engine", "event_broker", "subscription_service", "history_manager")}
        self.mocks = {name: p.start() for name, p in self.patches.items()}
        for name in ("crop_manager", "crop_report_manager", "user_crop_report_manager"):
            p = patch(f"routers.v2.wizard.{name}", _empty_manager()); p.start(); self.patches[name] = p
        self.mocks["event_broker"].publish = AsyncMock()
        self.mocks["subscription_service"].check_quota = AsyncMock()
        self.mocks["subscription_service"].confirm = AsyncMock(return_value=(True, {}))
        self.mocks["history_manager"].create = AsyncMock()
        self.mocks["scoring_engine"].recommend = AsyncMock(return_value=RecommendationResponse(results=[
            CropRecommendation(crop="Wheat", family="Cereal", horizon="SEASONAL", total=10, max_total=20,
                               suitability_pct=50, factors=[])]))

    async def asyncTearDown(self):
        for p in self.patches.values():
            p.stop()

    async def submit(self, livestock):
        from routers.v2.wizard import submit_wizard
        req = WizardSubmitRequest(farmId=None, subscription_id="sub_1", answers=WizardAnswersPayload(
            location=[73.85, 18.52], season="Rabi", irrigation=["Drip"], livestock=livestock))
        return await submit_wizard(req, lang="en", auth_payload={"upid": "usr_1"})

    async def test_several_kinds_reach_history_and_the_event_as_one_text_value(self):
        await self.submit([COWS, "Poultry farm"])

        self.assertEqual(self.mocks["history_manager"].create.call_args.args[0].livestock, f"{COWS}; Poultry farm")
        event = self.mocks["event_broker"].publish.call_args.args[1]
        self.assertEqual(event["data"]["livestock"], f"{COWS}; Poultry farm")

    async def test_no_livestock_with_others_is_400_before_any_credit_or_scoring(self):
        with self.assertRaises(HTTPException) as raised:
            await self.submit([NONE, "Fisheries"])

        self.assertEqual(raised.exception.status_code, 400)
        self.mocks["subscription_service"].check_quota.assert_not_awaited()
        self.mocks["scoring_engine"].recommend.assert_not_awaited()

    async def test_not_answered_sends_null(self):
        await self.submit(None)

        self.assertIsNone(self.mocks["event_broker"].publish.call_args.args[1]["data"]["livestock"])
