"""
Inserts the parsed Excel data (see excelSeedParser.py) into the crop reference
tables via the ORM managers. Used by tests/test_scoring_engine.py against a
throwaway local DB. NOT wired into app startup -- seeding the real database is
a separate, explicit step (see scripts/generate_seed_sql.py for the reviewable
SQL-file alternative used against the real dev DB).
"""
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker

from managers import (
    CropSchema, CropZoneScoreSchema, CropSeasonScoreSchema, CropWaterScoreSchema,
    CropSoilScoreSchema, BudgetRuleSchema, RotationRuleSchema, ScoringWeightSchema,
)
from utils.excelSeedParser import (
    load_workbook, crop_uid, parse_crops, parse_zone_scores, parse_season_scores,
    parse_water_scores, parse_soil_scores, parse_budget_rules, parse_rotation_rules, parse_weights,
)


async def seed_crop_reference_data(engine: AsyncEngine) -> None:
    wb = load_workbook()
    session_factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        for c in parse_crops(wb).values():
            zones_str = c["main_zones"] or ""
            mask = 0
            for z in str(zones_str).split(','):
                z = z.strip()
                if z.isdigit():
                    n = int(z)
                    if 1 <= n <= 15:
                        mask |= (1 << (n - 1))

            session.add(CropSchema(
                uid=crop_uid(c["code"]), code=c["code"], name_en=c["name"], family=c["family"],
                horizon=c["horizon"], water_need=c["water_need"], cost_class=c["cost_class"],
                main_zones_mask=mask, note=c["note"],
            ))
        await session.flush()

        for code, zone, score in parse_zone_scores(wb):
            session.add(CropZoneScoreSchema(
                uid=f"crop-zone-{code:03d}-{zone:02d}", crop_uid=crop_uid(code), zone=zone, score=int(score)))
        for code, season, score in parse_season_scores(wb):
            session.add(CropSeasonScoreSchema(
                uid=f"crop-season-{code:03d}-{season.lower()}", crop_uid=crop_uid(code), season=season,
                score=int(score)))
        for code, method, score in parse_water_scores(wb):
            session.add(CropWaterScoreSchema(
                uid=f"crop-water-{code:03d}-{method.lower()}", crop_uid=crop_uid(code), method=method,
                score=int(score)))
        for code, soil_class, score in parse_soil_scores(wb):
            session.add(CropSoilScoreSchema(
                uid=f"crop-soil-{code:03d}-{soil_class:02d}", crop_uid=crop_uid(code), soil_class=soil_class,
                score=int(score)))
        for bucket, cost_class, score in parse_budget_rules(wb):
            session.add(BudgetRuleSchema(
                uid=f"budget-{bucket.lower()}-{cost_class.lower()}", budget_bucket=bucket, cost_class=cost_class,
                score=int(score)))
        for situation, score in parse_rotation_rules(wb):
            session.add(RotationRuleSchema(
                uid=f"rotation-{situation.lower().replace('_', '-')}", situation=situation, score=int(score)))
        for factor, weight in parse_weights(wb):
            session.add(ScoringWeightSchema(uid=f"weight-{factor.lower()}", factor=factor, weight=float(weight)))

        await session.commit()


__all__ = ["seed_crop_reference_data"]
