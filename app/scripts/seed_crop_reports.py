# app/scripts/seed_crop_reports.py
"""Idempotent seed loader for the crop_reports master catalog (spec's
104-crop crops.json). Upserts by crop_id so re-running on every deploy
never duplicates or crashes on the unique constraint."""
import json
import logging

from managers.cropReportManagers import CropReportManager, CropReportSchema

logger = logging.getLogger("seed_crop_reports")

_FIELDS = (
    "crop_id", "name_en", "name_local", "family", "crop_type", "seasons",
    "duration_days", "years_to_first_harvest", "suitable_soil_types", "soil_texture",
    "soil_drainage", "soil_ph_min", "soil_ph_max", "water_requirement",
    "cost_per_acre_inr", "expected_yield_per_acre", "yield_unit", "profit_potential",
    "sowing_windows", "profit_note", "weather", "risks", "tips",
    "soil_economic_benefits", "hero_image_url", "icon_url", "is_active",
    "data_source", "review_status",
)


async def seed_crop_reports(manager: CropReportManager, session, crops_file_path: str) -> int:
    with open(crops_file_path, encoding="utf-8") as f:
        crops = json.load(f)

    count = 0
    for crop in crops:
        fields = {k: crop.get(k) for k in _FIELDS}
        existing = await manager.fetch_all(limit=1, filters={"crop_id": fields["crop_id"]}, session=session)
        if existing.items:
            await manager.update(uid=existing.items[0].uid, updates=fields, session=session)
        else:
            await manager.create(CropReportSchema(**fields), session=session)
        count += 1

    return count


if __name__ == "__main__":
    import asyncio
    from pathlib import Path
    from config import get_engine, get_settings

    async def _main():
        settings = get_settings()
        manager = CropReportManager(get_engine(settings.name))
        crops_path = Path(__file__).resolve().parents[1] / "data" / "crops.json"
        async with manager.session_factory() as session:
            n = await seed_crop_reports(manager, session, str(crops_path))
            await session.commit()
            logger.info(f"Seeded {n} crop_reports rows")

    asyncio.run(_main())
