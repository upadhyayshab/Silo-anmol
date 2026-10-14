"""
4-stage crop recommendation engine per reference_docs/version_2_docs.pdf:
translate -> eliminate (hard filters) -> score (soft factors) -> rank.

Zone resolution: lat/lon -> Google Maps API -> pincode -> agro-climatic zone.
Soil detection: lat/lon -> ISRIC S3 raster (SoilTextureLookup) -> USDA soil class.
Both run automatically from coordinates; neither is a user-supplied parameter.
"""
import asyncio
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncEngine

from managers import (
    CropManager, CropSeasonScoreManager, CropWaterScoreManager, CropSoilScoreManager,
    BudgetRuleManager, RotationRuleManager, ScoringWeightManager,
    CropZoneScoreManager, PincodeZoneMappingManager,
)
from models.recommendationModels import (
    RecommendationRequest, RecommendationResponse, CropRecommendation, FactorScore,
)
from services.locationService import LocationService
from utils.constants import RotationSituation, SOIL_TEXTURE_CLASSES
from utils.soilTexture import get_soil_texture_lookup

ZONE_SKIPPED_NOTE = (
    "Zone hard-filter skipped: no coordinates provided."
)


class CropScoringEngine:
    def __init__(self, engine: AsyncEngine):
        self.crop_manager = CropManager(engine)
        self.season_manager = CropSeasonScoreManager(engine)
        self.water_manager = CropWaterScoreManager(engine)
        self.soil_manager = CropSoilScoreManager(engine)
        self.budget_manager = BudgetRuleManager(engine)
        self.rotation_manager = RotationRuleManager(engine)
        self.weight_manager = ScoringWeightManager(engine)
        self.zone_manager = CropZoneScoreManager(engine)
        self.pincode_manager = PincodeZoneMappingManager(engine)
        self.location_service = LocationService()

    @staticmethod
    def _rotation_situation(request: RecommendationRequest, crop_family: str) -> str:
        if request.was_fallow:
            return RotationSituation.FALLOW
        if crop_family == request.previous_crop_family:
            return RotationSituation.SAME_FAMILY
        return RotationSituation.DIFFERENT_FAMILY

    async def _load_weights(self) -> dict[str, float]:
        rows = await self.weight_manager.fetch_all(limit=0)
        return {row.factor: row.weight for row in rows.items}

    async def _load_season_scores(self, season: str) -> dict[str, int]:
        rows = await self.season_manager.fetch_all(limit=0, filters={"season": season})
        return {row.crop_uid: row.score for row in rows.items}

    async def _load_water_scores(self, methods: list[str]) -> dict[str, int]:
        rows = await self.water_manager.fetch_all(limit=0, filters={"method": list(methods)})
        best: dict[str, int] = {}
        for row in rows.items:
            best[row.crop_uid] = max(best.get(row.crop_uid, 0), row.score)
        return best

    async def _load_soil_scores(self, soil_class: int) -> dict[str, int]:
        if not soil_class:
            return {}
        rows = await self.soil_manager.fetch_all(limit=0, filters={"soil_class": soil_class})
        return {row.crop_uid: row.score for row in rows.items}

    async def _load_budget_rules(self) -> dict[tuple[str, str], int]:
        rows = await self.budget_manager.fetch_all(limit=0)
        return {(row.budget_bucket, row.cost_class): row.score for row in rows.items}

    async def _load_rotation_rules(self) -> dict[str, int]:
        rows = await self.rotation_manager.fetch_all(limit=0)
        return {row.situation: row.score for row in rows.items}

    async def _load_zone_scores(self, zone: int) -> dict[str, int]:
        rows = await self.zone_manager.fetch_all(limit=0, filters={"zone": zone})
        return {row.crop_uid: row.score for row in rows.items}

    async def _detect_soil_class(self, lat: float, lon: float, notes: list[str]) -> int:
        """Run ISRIC raster lookup in a thread (rasterio is synchronous).
        Returns the detected USDA soil class (0 = no data / skip)."""
        try:
            lookup = get_soil_texture_lookup()
            result = await asyncio.to_thread(lookup.get_texture, lat, lon)
            if result.status == "ok" and result.soil_class:
                label = SOIL_TEXTURE_CLASSES.get(result.soil_class, "Unknown")
                notes.append(
                    f"Soil texture auto-detected: {label} (Class {result.soil_class}) from coordinates."
                )
                return result.soil_class
            elif result.status == "nodata":
                notes.append("Soil texture: no data at these coordinates (road/water/urban). Soil factor skipped.")
                return 0
            else:
                notes.append(f"Soil texture detection failed ({result.status}): {result.message}. Soil factor skipped.")
                return 0
        except Exception as exc:
            notes.append(f"Soil texture engine error: {exc}. Soil factor skipped.")
            return 0

    async def recommend(
        self,
        request: RecommendationRequest,
        lang: str = "en",
        auto_detect_soil: bool = False,
    ) -> RecommendationResponse:
        """Run the full recommendation pipeline.

        Args:
            request: Scoring inputs (soil_class may be 0 if auto_detect_soil=True).
            lang: ISO 639-1 language code for crop name translation.
            auto_detect_soil: If True, detect soil_class from request.lat/lon via ISRIC raster.
        """
        weights = await self._load_weights()

        # ── Zone resolution ──────────────────────────────────────────────────
        zone: Optional[int] = None
        zone_resolved = False
        notes: list[str] = []

        if request.lat is not None and request.lon is not None:
            pincode = await self.location_service.get_pincode_from_coordinates(request.lat, request.lon)
            if pincode:
                mapping = await self.pincode_manager.fetch_all(limit=1, filters={"pincode": pincode})
                if mapping.items:
                    zone = mapping.items[0].zone
                    zone_resolved = True
                    notes.append(f"Agro-climatic zone resolved to Zone {zone} (from pincode {pincode}).")
                else:
                    notes.append(f"Zone mapping not found for pincode {pincode}. Skipping zone filtering.")
            else:
                notes.append("Pincode could not be resolved from coordinates. Skipping zone filtering.")
        elif request.pincode:
            mapping = await self.pincode_manager.fetch_all(limit=1, filters={"pincode": request.pincode})
            if mapping.items:
                zone = mapping.items[0].zone
                zone_resolved = True
                notes.append(f"Agro-climatic zone resolved to Zone {zone} (from pincode {request.pincode}).")
            else:
                notes.append(f"Zone mapping not found for pincode {request.pincode}. Skipping zone filtering.")
        else:
            notes.append(ZONE_SKIPPED_NOTE)

        # ── Soil auto-detection ───────────────────────────────────────────────
        soil_class = request.soil_class
        soil_class_detected: Optional[int] = None
        soil_texture_label: Optional[str] = None

        if auto_detect_soil and request.lat is not None and request.lon is not None:
            soil_class = await self._detect_soil_class(request.lat, request.lon, notes)
            soil_class_detected = soil_class
            soil_texture_label = SOIL_TEXTURE_CLASSES.get(soil_class, "Unknown")

        # ── Load scoring tables ───────────────────────────────────────────────
        season_scores = await self._load_season_scores(request.season)
        water_scores = await self._load_water_scores(request.water_methods)
        soil_scores = await self._load_soil_scores(soil_class)
        budget_rules = await self._load_budget_rules()
        rotation_rules = await self._load_rotation_rules()

        zone_scores: dict[str, int] = {}
        if zone_resolved and zone is not None:
            zone_scores = await self._load_zone_scores(zone)

        crops = (await self.crop_manager.fetch_all(limit=0)).items

        soil_applicable = bool(soil_class)
        applicable_factors = ["SEASON", "WATER", "BUDGET", "ROTATION"]
        if soil_applicable:
            applicable_factors.append("SOIL")
        if zone_resolved:
            applicable_factors.append("ZONE")

        # Each factor's ceiling is 2 points (an "ideal match") x its weight.
        max_total = sum(weights[factor] * 2 for factor in applicable_factors)

        # ── Score & rank ──────────────────────────────────────────────────────
        results: list[CropRecommendation] = []
        for crop in crops:
            # Hard filter: zone
            if zone_resolved:
                zone_pts = zone_scores.get(crop.uid, 0)
                if not zone_pts:
                    continue  # hard filter: crop cannot grow in this zone

            season_pts = season_scores.get(crop.uid, 0)
            if not season_pts:
                continue  # hard filter: crop can't be planted this season
            water_pts = water_scores.get(crop.uid, 0)
            if not water_pts:
                continue  # hard filter: none of the farmer's methods can water this crop

            factors = [
                FactorScore(factor="SEASON", points=season_pts, weight=weights["SEASON"],
                            weighted=season_pts * weights["SEASON"]),
                FactorScore(factor="WATER", points=water_pts, weight=weights["WATER"],
                            weighted=water_pts * weights["WATER"]),
            ]

            if zone_resolved:
                factors.append(FactorScore(factor="ZONE", points=zone_pts, weight=weights["ZONE"],
                                           weighted=zone_pts * weights["ZONE"]))

            if soil_applicable:
                soil_pts = soil_scores.get(crop.uid, 0)
                factors.append(FactorScore(factor="SOIL", points=soil_pts, weight=weights["SOIL"],
                                           weighted=soil_pts * weights["SOIL"]))

            budget_pts = budget_rules.get((request.budget_bucket, crop.cost_class), 0)
            factors.append(FactorScore(factor="BUDGET", points=budget_pts, weight=weights["BUDGET"],
                                       weighted=budget_pts * weights["BUDGET"]))

            situation = self._rotation_situation(request, crop.family)
            rotation_pts = rotation_rules.get(situation, 0)
            factors.append(FactorScore(factor="ROTATION", points=rotation_pts, weight=weights["ROTATION"],
                                       weighted=rotation_pts * weights["ROTATION"]))

            total = sum(f.weighted for f in factors)
            suitability = round(total / max_total * 100, 1) if max_total else 0.0

            results.append(CropRecommendation(
                crop=crop.name_en, family=crop.family, horizon=crop.horizon,
                translated_name=(getattr(crop, f"name_{lang}", None) if lang != "en" else None),
                total=total, max_total=max_total, suitability_pct=suitability, factors=factors,
            ))

        results.sort(key=lambda r: r.total, reverse=True)
        top_results = results[:request.top_n]

        return RecommendationResponse(
            zone_filter_applied=zone_resolved,
            soil_class_detected=soil_class_detected,
            soil_texture_label=soil_texture_label,
            notes=notes,
            results=top_results,
        )


__all__ = ["CropScoringEngine"]
