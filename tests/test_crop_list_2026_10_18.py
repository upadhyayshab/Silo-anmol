"""One crop list for both features (2026-10-18): 36 crops added to the
engine (migration i1a2b3c4d5e6 + crops.json; 35 since Abaca was dropped), and every crop on Agro's list
is understood. The engine runs here on the real seed data (104 crops from
migrations/data/seed_crop_reference.sql plus the 35 from the migration)."""
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

SEED_SQL = (ROOT / "migrations" / "data" / "seed_crop_reference.sql").read_text()
PAGES = json.loads((ROOT / "app" / "data" / "crops.json").read_text())
EXISTING_PAGES = {p["crop_id"]: p for p in PAGES[:104]}
AGRO_NAMES = json.loads((ROOT / "tests" / "fixtures" / "agro_crop_names_2026_10_18.json").read_text())["crop_names"]


def _migration():
    spec = importlib.util.spec_from_file_location("i1", ROOT / "migrations" / "versions" / "i1a2b3c4d5e6_add_35_scored_crops.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_rows(table: str) -> list[dict]:
    rows = []
    for m in re.finditer(rf"INSERT INTO \w+\.{table} \(([^)]*)\) VALUES \((.*)\);", SEED_SQL):
        cols = [c.strip() for c in m.group(1).split(",")]
        values = [v.strip().strip("'") for v in re.split(r",(?=(?:[^']*'[^']*')*[^']*$)", m.group(2))]
        rows.append(dict(zip(cols, values)))
    return rows


def _all_scoring_data():
    """crops + score rows for all 139 crops, as the database holds them after i1."""
    crops = [NS(uid=r["uid"], name_en=r["name"], family=r["family"], horizon=r["horizon"],
                water_need=r["water_need"], cost_class=r["cost_class"]) for r in _seed_rows("crops")]
    tables = {t: [NS(**{k: (int(v) if k in ("zone", "score", "soil_class") else v) for k, v in r.items()})
                  for r in _seed_rows(t)]
              for t in ("crop_zone_scores", "crop_season_scores", "crop_water_scores", "crop_soil_scores")}
    for crop in _migration().CROPS:
        row, zones, seasons, water, soil = _migration()._rows(crop)
        crops.append(NS(**row))
        for table, new in zip(tables, (zones, seasons, water, soil)):
            tables[table].extend(NS(**r) for r in new)
    return crops, tables


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    async def fetch_all(self, limit=0, filters=None, **kwargs):
        rows = self.rows
        for key, wanted in (filters or {}).items():
            allowed = wanted if isinstance(wanted, list) else [wanted]
            rows = [r for r in rows if getattr(r, key) in allowed]
        return NS(items=rows, count=len(rows))


class TestNewCropData(TestCase):
    def test_35_crops_with_complete_scores(self):
        m = _migration()
        self.assertEqual(m.down_revision, "h1a2b3c4d5e6")
        self.assertEqual(len(m.CROPS), 35)
        self.assertEqual([c["code"] for c in m.CROPS], list(range(105, 140)))
        seed_names = {r["name"].lower() for r in _seed_rows("crops")}
        families = {r["family"] for r in _seed_rows("crops")}
        for crop in m.CROPS:
            self.assertNotIn(crop["name_en"].lower(), seed_names)
            self.assertIn(crop["family"], families)
            self.assertTrue(crop["zones"] and set(crop["zones"]) <= set(range(1, 16)), crop["name_en"])
            self.assertEqual(set(crop["season"]), {"KHARIF", "RABI", "ZAID", "LONGTERM"})
            self.assertEqual(set(crop["water"]), {"RAINFED", "SURFACE", "DRIP", "SPRINKLER"})
            self.assertTrue(re.fullmatch(r"[012]{12}", crop["soil"]))
            self.assertTrue(any(crop["season"].values()) and any(crop["water"].values()))
            self.assertTrue(all(isinstance(v, str) and v for v in crop["names"].values()), crop["name_en"])
            row, zones, seasons, water, soil = m._rows(crop)
            self.assertEqual((len(zones), len(seasons), len(water), len(soil)), (15, 4, 4, 12))

    def test_new_local_names_are_not_another_crops_name(self):
        """CropCatalog treats a name two crops share as ambiguous and finds
        neither, so a new crop reusing e.g. বাদাম would break Groundnut."""
        fixture = ROOT / "tests" / "fixtures" / "crop_local_names_104_2026_10_18.json"
        rows = json.loads(fixture.read_text())["crops"] + [
            {"name_en": c["name_en"], **c["names"]} for c in _migration().CROPS
        ]
        new = {c["name_en"] for c in _migration().CROPS}
        owners: dict[str, set] = {}
        for row in rows:
            for column, value in row.items():
                if column == "name_en" or not isinstance(value, str) or not value.strip():
                    continue
                for key in {value.strip().lower(), *(p.strip().lower() for p in value.split("/") if p.strip())}:
                    owners.setdefault(key, set()).add(row["name_en"])
        shared = {k: v for k, v in owners.items() if len(v) > 1 and v & new}
        self.assertEqual(shared, {})

    def test_info_page_for_every_new_crop_in_the_existing_format(self):
        self.assertEqual(len(PAGES), 139)
        self.assertEqual(len({p["crop_id"] for p in PAGES}), 139)
        allowed = {key: {v for p in EXISTING_PAGES.values() for v in (p[key] if isinstance(p[key], list) else [p[key]])}
                   for key in ("crop_type", "seasons", "suitable_soil_types", "soil_texture", "soil_drainage",
                               "water_requirement", "yield_unit", "profit_potential")}
        risk_types = {r["type"] for p in EXISTING_PAGES.values() for r in p["risks"]}
        tip_categories = {t["category"] for p in EXISTING_PAGES.values() for t in p["tips"]}
        tip_icons = {t["icon"] for p in EXISTING_PAGES.values() for t in p["tips"]}
        for page in PAGES[104:]:
            name = page["crop_id"]
            existing_keys = {k for p in EXISTING_PAGES.values() for k in p}
            self.assertLessEqual(set(page), existing_keys, name)
            self.assertLessEqual(existing_keys - {"duration_days", "years_to_first_harvest"}, set(page), name)
            for key, values in allowed.items():
                got = page[key] if isinstance(page[key], list) else [page[key]]
                self.assertLessEqual(set(got), values, (name, key))
            self.assertEqual([w["param"] for w in page["weather"]], ["temperature", "rainfall", "humidity"])
            for w in page["weather"]:
                self.assertTrue(w["tolerable_min"] <= w["ideal_min"] <= w["ideal_max"] <= w["tolerable_max"], (name, w))
            self.assertGreaterEqual(len(page["risks"]), 3)
            self.assertGreaterEqual(len(page["tips"]), 3)
            self.assertLessEqual({r["type"] for r in page["risks"]}, risk_types, name)
            self.assertLessEqual({t["category"] for t in page["tips"]}, tip_categories, name)
            self.assertLessEqual({t["icon"] for t in page["tips"]}, tip_icons, name)
            self.assertTrue(page["name_local"]["hi"] and page["name_local"]["mr"], name)
            self.assertEqual(page["review_status"], "ai_draft")
            self.assertTrue(("duration_days" in page) != ("years_to_first_harvest" in page), name)


class TestEveryAgroCropUnderstood(IsolatedAsyncioTestCase):
    async def _catalog(self):
        from services.cropCatalog import CropCatalog

        crops, _ = _all_scoring_data()
        reports = [NS(crop_id=p["crop_id"], name_en=p["name_en"], name_local=p["name_local"]) for p in PAGES]
        return CropCatalog(_Rows(reports), _Rows(crops))

    async def test_every_agro_crop_is_recommendable_except_hemp_abaca_mushrooms(self):
        catalog = await self._catalog()
        engine_only = [r["name"] for r in _seed_rows("crops")]  # Agro migration q1 adds those missing from its list
        not_scored = {n.strip() for n in AGRO_NAMES + engine_only if await catalog.find_scoring(n) is None}
        self.assertEqual(not_scored, {"Hemp", "Mushrooms", "Abaca (Manila hemp)"})
        for name in AGRO_NAMES + engine_only:
            self.assertIsNotNone(await catalog.find_report(name) or name.strip() in not_scored, name)

    async def test_families_and_aliases(self):
        catalog = await self._catalog()
        self.assertEqual(await catalog.find_family("Hemp"), "Fibre")
        self.assertEqual(await catalog.find_family("Abaca (Manila hemp)"), "Fibre")
        self.assertIsNone(await catalog.find_family("Mushrooms"))
        cases = {"Cantaloupe": "muskmelon", "Anise seeds": "fennel", "Gooseberry": "amla", "Beet, sugar  ": "sugar_beet",
                 "Blackberries": "blackberry", "Dates": "date_palm", "Cacao (cocoa)": "cacao", "Fenugreek": "fenugreek_leaf",
                 "Orange": "sweet_orange"}
        for agro_name, crop_id in cases.items():
            self.assertEqual((await catalog.find_report(agro_name)).crop_id, crop_id, agro_name)
        self.assertEqual((await catalog.find_scoring("Oats")).family, "Fodder")


class TestEngineWithNewCrops(IsolatedAsyncioTestCase):
    def _engine(self, zone):
        from utils.scoringEngine import CropScoringEngine

        crops, tables = _all_scoring_data()
        engine = CropScoringEngine.__new__(CropScoringEngine)
        engine.crop_manager = _Rows(crops)
        engine.season_manager = _Rows(tables["crop_season_scores"])
        engine.water_manager = _Rows(tables["crop_water_scores"])
        engine.soil_manager = _Rows(tables["crop_soil_scores"])
        engine.zone_manager = _Rows(tables["crop_zone_scores"])
        engine.budget_manager = _Rows([NS(budget_bucket=r["budget_bucket"], cost_class=r["cost_class"], score=int(r["score"]))
                                       for r in _seed_rows("budget_rules")])
        engine.rotation_manager = _Rows([NS(situation=r["situation"], score=int(r["score"])) for r in _seed_rows("rotation_rules")])
        engine.weight_manager = _Rows([NS(factor=r["factor"], weight=float(r["weight"])) for r in _seed_rows("scoring_weights")])
        engine.pincode_manager = _Rows([NS(pincode="000001", zone=zone)])
        engine.location_service = MagicMock(get_pincode_from_coordinates=AsyncMock(return_value=None))
        return engine

    async def _recommend(self, zone, season, methods):
        """Every crop that passes the hard filters, not just the top 20."""
        from models.recommendationModels import RecommendationRequest

        request = RecommendationRequest(lat=None, lon=None, pincode="000001", season=season, water_methods=methods,
                                        budget_bucket="B4", was_fallow=True)
        request.top_n = 139
        return [r.crop for r in (await self._engine(zone).recommend(request)).results]

    async def test_hill_farmer_can_get_temperate_fruits(self):
        crops = await self._recommend(zone=1, season="LONGTERM", methods=["DRIP"])
        self.assertTrue({"Apple", "Walnut", "Cherry", "Saffron"} <= set(crops), crops)

    async def test_pune_farmer_never_gets_hill_crops(self):
        crops = await self._recommend(zone=9, season="LONGTERM", methods=["DRIP"])
        self.assertFalse({"Apple", "Cherry", "Walnut", "Saffron", "Almond"} & set(crops))
        self.assertIn("Fig", crops)

    async def test_rabi_crops_in_punjab(self):
        crops = await self._recommend(zone=6, season="RABI", methods=["SURFACE"])
        self.assertTrue({"Oats", "Sugar beet", "Celery", "Broccoli", "Wheat"} <= set(crops), crops)
        self.assertNotIn("Mint", crops)  # a summer crop

    async def test_mint_is_a_summer_crop_and_needs_irrigation(self):
        self.assertIn("Mint", await self._recommend(zone=5, season="ZAID", methods=["SURFACE"]))
        self.assertNotIn("Mint", await self._recommend(zone=5, season="ZAID", methods=["RAINFED"]))
