"""Assembles the three report/dossier read models from crop_reports (master
catalog) and user_crop_reports (per-run cache). No endpoint-layer logic
here — routers/v2/reports.py (Task 9) just calls these three methods."""
import logging
from datetime import datetime, timezone

from managers.cropReportManagers import CropReportManager, UserCropReportManager
from models.reportModels import (
    CropRisk, CropTip, SingleCropDossier, SoilEconomicBenefits, SowingWindow,
    UnifiedCropReportResponse, UserReportSummaryCard, UserReportsListResponse,
    RecommendedCropSummaryItem, WeatherMetric,
)

logger = logging.getLogger("crop_report_service")


class CropReportService:
    def __init__(self, crop_report_manager: CropReportManager, user_crop_report_manager: UserCropReportManager):
        self.crop_report_manager = crop_report_manager
        self.user_crop_report_manager = user_crop_report_manager

    async def _find_master_row(self, name_en: str):
        result = await self.crop_report_manager.fetch_all(limit=1, filters={"name_en": name_en})
        if result.items:
            return result.items[0]
        all_rows = await self.crop_report_manager.fetch_all(limit=0)
        return next((r for r in all_rows.items if r.name_en.lower() == name_en.lower()), None)

    @staticmethod
    def _to_dossier(master_row, match_score: int, is_top_pick: bool, match_reasons: list[str]) -> SingleCropDossier:
        return SingleCropDossier(
            crop_id=master_row.crop_id, name_en=master_row.name_en, name_local=master_row.name_local,
            match_score=match_score, is_top_pick=is_top_pick, match_reasons=match_reasons,
            family=master_row.family, crop_type=master_row.crop_type, seasons=master_row.seasons,
            duration_days=master_row.duration_days,
            years_to_first_harvest=float(master_row.years_to_first_harvest) if master_row.years_to_first_harvest else None,
            cost_per_acre_inr=master_row.cost_per_acre_inr,
            expected_yield_per_acre=float(master_row.expected_yield_per_acre),
            yield_unit=master_row.yield_unit, profit_potential=master_row.profit_potential,
            profit_note=master_row.profit_note,
            sowing_windows=[SowingWindow(**w) for w in master_row.sowing_windows],
            weather=[WeatherMetric(**w) for w in master_row.weather],
            risks=[CropRisk(**r) for r in master_row.risks],
            tips=[CropTip(**t) for t in master_row.tips],
            soil_economic_benefits=SoilEconomicBenefits(**master_row.soil_economic_benefits),
            hero_image_url=master_row.hero_image_url, icon_url=master_row.icon_url,
        )

    async def get_master_report(self, crop_id: str, lang: str = "en") -> SingleCropDossier:
        result = await self.crop_report_manager.fetch_all(limit=1, filters={"crop_id": crop_id})
        if not result.items:
            raise ValueError(f"No crop_reports row for crop_id '{crop_id}'")
        return self._to_dossier(result.items[0], match_score=0, is_top_pick=False, match_reasons=[])

    async def get_or_create_unified_report(
        self, recommendation_id: str, user_id: str, farm_id: str | None = None,
        lang: str = "en", focus_crop_id: str | None = None,
    ) -> UnifiedCropReportResponse:
        runs = await self.user_crop_report_manager.fetch_all(
            limit=1, filters={"recommendation_id": recommendation_id, "user_id": user_id},
        )
        if not runs.items:
            raise ValueError(f"No recommendation run '{recommendation_id}' for user '{user_id}'")
        run = runs.items[0]

        ranked = run.report_data.get("results", [])
        dossiers: list[SingleCropDossier] = []
        for crop_result in ranked:
            master_row = await self._find_master_row(crop_result["crop"])
            if master_row is None:
                logger.warning(f"No crop_reports row for scored crop '{crop_result['crop']}' — skipping dossier")
                continue
            # is_top_pick is based on position within the *surviving* dossiers list, not the
            # original ranked index — if the true top-ranked crop has no crop_reports row,
            # the first dossier that actually makes it into `crops` is the one flagged.
            dossiers.append(self._to_dossier(
                master_row, match_score=round(crop_result["suitability_pct"]), is_top_pick=(len(dossiers) == 0),
                match_reasons=[f"Suitability {crop_result['suitability_pct']:.0f}% for {run.season.title()} season."],
            ))

        # top_crop_id/name/score must all describe the same crop. Derive them from the first
        # surviving dossier (same source that drives is_top_pick above) rather than trusting
        # run.top_crop_name/top_crop_score, which describe the original ranked-list top crop
        # and may refer to a crop that didn't survive the master-catalog lookup.
        top_dossier = dossiers[0] if dossiers else None

        return UnifiedCropReportResponse(
            report_id=run.uid, recommendation_id=run.recommendation_id, user_id=run.user_id,
            farm_id=run.farm_id, farm_name=run.farm_name, season=run.season, language=lang,
            top_crop_id=top_dossier.crop_id if top_dossier else "",
            top_crop_name=top_dossier.name_en if top_dossier else run.top_crop_name,
            top_crop_score=top_dossier.match_score if top_dossier else run.top_crop_score,
            total_recommended_crops=len(ranked), crops=dossiers, more_for_your_farm=[],
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    async def list_user_reports(
        self, user_id: str, farm_id: str | None = None,
        recommendation_id: str | None = None, lang: str = "en",
    ) -> UserReportsListResponse:
        filters = {"user_id": user_id}
        if farm_id:
            filters["farm_id"] = farm_id
        if recommendation_id:
            filters["recommendation_id"] = recommendation_id
        runs = await self.user_crop_report_manager.fetch_all(limit=0, filters=filters, sorts=["-created_at"])

        cards = [
            UserReportSummaryCard(
                report_id=run.uid, recommendation_id=run.recommendation_id, farm_id=run.farm_id,
                farm_name=run.farm_name, season=run.season, top_crop_id=run.top_crop_name.lower().replace(" ", "_"),
                top_crop_name=run.top_crop_name, top_crop_vernacular=run.top_crop_name,
                top_crop_score=run.top_crop_score, total_recommended_crops=run.total_recommendations,
                recommended_crops_summary=[
                    RecommendedCropSummaryItem(
                        crop_id=r["crop"].lower().replace(" ", "_"), crop_name=r["crop"],
                        crop_name_vernacular=r["crop"], match_score=round(r["suitability_pct"]),
                        is_top_pick=(i == 0),
                    )
                    for i, r in enumerate(run.report_data.get("results", []))
                ],
                created_at=run.created_at.isoformat() if run.created_at else "",
            )
            for run in runs.items
        ]
        return UserReportsListResponse(user_id=user_id, total=len(cards), reports=cards)


__all__ = ["CropReportService"]
