"""Assembles the three report/dossier read models from crop_reports (master
catalog) and user_crop_reports (per-run cache). No endpoint-layer logic
here — routers/v2/reports.py (Task 9) just calls these three methods."""
import logging
from datetime import datetime, timezone
from typing import Optional

from managers.cropReportManagers import CropReportManager, UserCropReportManager
from models.reportModels import (
    CropRisk, CropTip, MoreForYourFarmItem, SingleCropDossier, SoilEconomicBenefits, SowingWindow,
    UnifiedCropReportResponse, UserReportSummaryCard, UserReportsListResponse,
    RecommendedCropSummaryItem, WeatherMetric,
)
from services.cropCatalog import CropCatalog, crop_slug
from utils.farmerText import suitability_reason
from utils.languages import normalize_lang
from utils.matchReasons import match_reasons

logger = logging.getLogger("crop_report_service")


class CropReportService:
    def __init__(
        self, crop_report_manager: CropReportManager, user_crop_report_manager: UserCropReportManager,
        catalog: Optional[CropCatalog] = None, crop_manager=None,
    ):
        self.crop_report_manager = crop_report_manager
        self.user_crop_report_manager = user_crop_report_manager
        # Scoring-engine names ('Lime/lemon') differ from catalog names
        # ('Lime / Lemon') for 7 crops; the catalog resolves both, and loads
        # the 104 rows once instead of one query (or full scan) per crop.
        # With crop_manager it also has the crops table's names in all 13
        # languages (crops.json only has hi/mr).
        self.catalog = catalog or CropCatalog(crop_report_manager, crop_manager)

    async def _find_master_row(self, name_en: str):
        return await self.catalog.find_report(name_en)

    async def _to_dossier(self, master_row, match_score: int, is_top_pick: bool, match_reasons: list[str]) -> SingleCropDossier:
        return SingleCropDossier(
            crop_id=master_row.crop_id, name_en=master_row.name_en, name_local=await self.catalog.local_names(master_row),
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
        """Accepts the crop id, any spelling of its English name, or its
        local-language name."""
        master_row = await self.catalog.find_report(crop_id)
        if master_row is None:
            raise ValueError(f"No crop_reports row for crop_id '{crop_id}'")
        return await self._to_dossier(master_row, match_score=0, is_top_pick=False, match_reasons=[])

    async def get_or_create_unified_report(
        self, recommendation_id: str, user_id: str, farm_id: str | None = None,
        lang: str | None = None, focus_crop_id: str | None = None,
    ) -> UnifiedCropReportResponse:
        runs = await self.user_crop_report_manager.fetch_all(
            limit=1, filters={"recommendation_id": recommendation_id, "user_id": user_id},
        )
        if not runs.items:
            raise ValueError(f"No recommendation run '{recommendation_id}' for user '{user_id}'")
        run = runs.items[0]
        report_data = run.report_data or {}
        # No ?lang: the language the farmer ran the wizard in.
        lang = normalize_lang(lang or getattr(run, "language", None))
        previous_crop = await self._local_crop_name(report_data.get("previous_crop"), lang)

        ranked = report_data.get("results", [])
        dossiers: list[SingleCropDossier] = []
        for crop_result in ranked:
            master_row = await self._find_master_row(crop_result["crop"])
            if master_row is None:
                logger.warning(f"No crop_reports row for scored crop '{crop_result['crop']}' — skipping dossier")
                continue
            reasons = match_reasons(crop_result.get("factors"), run.season, previous_crop, lang=lang) or [
                suitability_reason(crop_result["suitability_pct"], run.season, lang)
            ]
            # is_top_pick is based on position within the *surviving* dossiers list, not the
            # original ranked index — if the true top-ranked crop has no crop_reports row,
            # the first dossier that actually makes it into `crops` is the one flagged.
            dossiers.append(await self._to_dossier(
                master_row, match_score=round(crop_result["suitability_pct"]), is_top_pick=(len(dossiers) == 0),
                match_reasons=reasons,
            ))

        # top_crop_id/name/score must all describe the same crop. Derive them from the first
        # surviving dossier (same source that drives is_top_pick above) rather than trusting
        # run.top_crop_name/top_crop_score, which describe the original ranked-list top crop
        # and may refer to a crop that didn't survive the master-catalog lookup.
        top_dossier = dossiers[0] if dossiers else None

        # The crop the farmer tapped is shown first; the engine's top pick
        # keeps its badge and top_crop_* (a crop the farmer chose to read
        # about isn't thereby the best match).
        focused_crop_id = None
        if focus_crop_id:
            focus_row = await self.catalog.find_report(focus_crop_id)
            wanted = focus_row.crop_id if focus_row else crop_slug(focus_crop_id)
            index = next((i for i, d in enumerate(dossiers) if d.crop_id == wanted), None)
            if index is None:
                raise ValueError(f"Crop '{focus_crop_id}' is not in recommendation run '{recommendation_id}'")
            dossiers.insert(0, dossiers.pop(index))
            focused_crop_id = wanted

        # GAP-09: "More for your farm" surfaces every dossier after the one
        # shown first -- same surviving list `dossiers` already assembled above.
        more_for_your_farm = [
            MoreForYourFarmItem(
                crop_id=d.crop_id, name_en=d.name_en, name_local=d.name_local,
                match_score=d.match_score, is_top_pick=d.is_top_pick, profit_note=d.profit_note,
            )
            for d in dossiers[1:]
        ]

        return UnifiedCropReportResponse(
            report_id=run.uid, recommendation_id=run.recommendation_id, user_id=run.user_id,
            farm_id=run.farm_id, farm_name=run.farm_name, season=run.season, language=lang,
            sowing_date=report_data.get("sowing_date"),
            top_crop_id=top_dossier.crop_id if top_dossier else "",
            top_crop_name=top_dossier.name_en if top_dossier else run.top_crop_name,
            top_crop_score=top_dossier.match_score if top_dossier else run.top_crop_score,
            total_recommended_crops=len(ranked), crops=dossiers, more_for_your_farm=more_for_your_farm,
            created_at=self._created_at(run),
            focused_crop_id=focused_crop_id,
        )

    @staticmethod
    def _created_at(run) -> str:
        """When the run was made -- not when the report was opened."""
        created = getattr(run, "created_at", None)
        if isinstance(created, datetime):
            return created.isoformat()
        return datetime.now(timezone.utc).isoformat()

    async def list_user_reports(
        self, user_id: str, farm_id: str | None = None,
        recommendation_id: str | None = None, lang: str | None = None,
    ) -> UserReportsListResponse:
        filters = {"user_id": user_id}
        if farm_id:
            filters["farm_id"] = farm_id
        if recommendation_id:
            filters["recommendation_id"] = recommendation_id
        # Newest first: SharedBackend reads "+" as descending, "-" as ascending.
        runs = await self.user_crop_report_manager.fetch_all(limit=0, filters=filters, sorts=["+created_at"])

        cards = []
        for run in runs.items:
            run_lang = normalize_lang(lang or getattr(run, "language", None))
            summaries = []
            for i, r in enumerate(run.report_data.get("results", [])):
                master_row = await self._find_master_row(r["crop"])
                local_name = (await self.catalog.local_name(master_row, run_lang) if master_row else None) or r["crop"]
                summaries.append(RecommendedCropSummaryItem(
                    crop_id=await self._resolve_crop_id(r["crop"]), crop_name=r["crop"],
                    crop_name_vernacular=local_name, match_score=round(r["suitability_pct"]),
                    is_top_pick=(i == 0),
                ))

            top_master = await self._find_master_row(run.top_crop_name)
            top_local_name = (await self.catalog.local_name(top_master, run_lang) if top_master else None) or run.top_crop_name
            thumbnail_url = (top_master.icon_url or top_master.hero_image_url) if top_master else None

            cards.append(UserReportSummaryCard(
                report_id=run.uid, recommendation_id=run.recommendation_id, farm_id=run.farm_id,
                farm_name=run.farm_name, season=run.season, top_crop_id=await self._resolve_crop_id(run.top_crop_name),
                top_crop_name=run.top_crop_name, top_crop_vernacular=top_local_name,
                top_crop_score=run.top_crop_score, total_recommended_crops=run.total_recommendations,
                recommended_crops_summary=summaries, thumbnail_url=thumbnail_url,
                created_at=run.created_at.isoformat() if run.created_at else "",
            ))
        return UserReportsListResponse(user_id=user_id, total=len(cards), reports=cards)

    async def _local_crop_name(self, crop_name: str | None, lang: str) -> str | None:
        """A crop's name in `lang` (for reason text), else as given."""
        if not crop_name or lang == "en":
            return crop_name
        master_row = await self._find_master_row(crop_name)
        return (await self.catalog.local_name(master_row, lang) if master_row else None) or crop_name

    async def _resolve_crop_id(self, crop_name: str) -> str:
        """The real crop_reports.crop_id doesn't always follow the naive
        `name.lower().replace(" ", "_")` slug rule (e.g. 'Rice (Paddy)'), so
        resolve it through the same master-catalog lookup used elsewhere in
        this class, falling back to the naive slug only when no catalog row
        exists for that crop name (Review finding Important #3)."""
        master_row = await self._find_master_row(crop_name)
        if master_row is not None:
            return master_row.crop_id
        return crop_name.lower().replace(" ", "_")


__all__ = ["CropReportService"]
