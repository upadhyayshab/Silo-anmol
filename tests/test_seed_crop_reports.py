"""Review Focus: seeding must be idempotent — upsert by crop_id, not
insert-and-crash on the second run."""
import json
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
import tempfile
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from scripts.seed_crop_reports import seed_crop_reports  # noqa: E402


class TestSeedCropReports(IsolatedAsyncioTestCase):
    def _write_sample_file(self, tmp_path):
        sample = [
            {
                "crop_id": "amla", "name_en": "Amla", "name_local": {"hi": "आंवला"},
                "family": "Fruit", "crop_type": "perennial", "seasons": ["kharif"],
                "years_to_first_harvest": 4, "suitable_soil_types": ["alluvial"],
                "soil_texture": ["loam"], "soil_drainage": "well_drained",
                "soil_ph_min": 6.5, "soil_ph_max": 9.0, "water_requirement": "low",
                "cost_per_acre_inr": 25000, "expected_yield_per_acre": 60,
                "yield_unit": "quintal", "profit_potential": "moderate",
                "sowing_windows": [{"season": "kharif", "start": "07-01", "end": "08-15"}],
                "profit_note": "note", "weather": [], "risks": [], "tips": [],
                "soil_economic_benefits": {"title": "t", "soil": [], "economic": []},
                "hero_image_url": None, "icon_url": None, "is_active": True,
                "data_source": "src", "review_status": "ai_draft",
            }
        ]
        file_path = tmp_path / "crops.json"
        file_path.write_text(json.dumps(sample), encoding="utf-8")
        return str(file_path)

    async def test_first_run_creates_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = self._write_sample_file(Path(tmp))
            manager = MagicMock()
            manager.fetch_all = AsyncMock(return_value=MagicMock(items=[]))  # no existing row
            manager.create = AsyncMock()
            session = AsyncMock()

            count = await seed_crop_reports(manager, session, file_path)

            self.assertEqual(count, 1)
            manager.create.assert_awaited_once()
            created_row = manager.create.call_args.args[0]
            self.assertEqual(created_row.crop_id, "amla")

    async def test_second_run_updates_not_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = self._write_sample_file(Path(tmp))
            existing_row = MagicMock(uid="existing-uid-1", crop_id="amla")
            manager = MagicMock()
            manager.fetch_all = AsyncMock(return_value=MagicMock(items=[existing_row]))
            manager.update = AsyncMock()
            manager.create = AsyncMock()
            session = AsyncMock()

            count = await seed_crop_reports(manager, session, file_path)

            self.assertEqual(count, 1)
            manager.create.assert_not_called()
            manager.update.assert_awaited_once()
            self.assertEqual(manager.update.call_args.kwargs["uid"], "existing-uid-1")
