from datetime import datetime
from typing import Any, Optional, Union

import sqlalchemy as db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from config import get_settings, get_engine
from managers.diseaseReportManager import DiseaseReportV1Schema
from managers.manureReportManager import ManureReportV1Schema
from models.reportModels import (
    ScanType,
    UnifiedDiseaseReportItem,
    UnifiedManureReportItem,
    ReportsListResponse,
)


class ReportService:
    def __init__(self):
        settings = get_settings()
        self.engine = get_engine(settings.name)
        self.session_factory = sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @staticmethod
    def _format_disease_report(record: DiseaseReportV1Schema) -> UnifiedDiseaseReportItem:
        findings = record.findings or []
        call_vet = any(
            isinstance(f, dict) and (
                f.get("severity") in ("Moderate", "Severe", "Critical")
                or "emergency" in str(f.get("severity_and_urgency", "")).lower()
            )
            for f in findings
        )
        return UnifiedDiseaseReportItem(
            report_id=record.uid,
            scan_type="disease_diagnosis",
            user_id=record.user_id,
            cow_id=record.cow_id,
            lang_code=record.lang_code or "en",
            img_url=record.img_url,
            created_at=record.created_at,
            title=record.primary_diagnosis or "Disease Diagnosis",
            primary_diagnosis=record.primary_diagnosis,
            detected_count=record.detected_count or 0,
            suspected_count=record.suspected_count or 0,
            call_vet=call_vet,
            is_cattle_image=bool(record.is_cattle_image),
            image_view=record.image_view,
            findings=findings,
            reasoning=record.reasoning,
            model=record.model,
            pr={},
        )

    @staticmethod
    def _format_manure_report(record: ManureReportV1Schema) -> UnifiedManureReportItem:
        data_meta = record.data_meta_json or {}
        return UnifiedManureReportItem(
            report_id=record.uid,
            scan_type="cow_dung",
            user_id=record.user_id,
            cow_id=record.cow_id,
            lang_code=record.lang_code or "en",
            img_url=record.img_url,
            created_at=record.created_at,
            title=record.title or f"Manure Score {record.manure_score}",
            manure_score=int(record.manure_score),
            manure_sub_score=record.manure_sub_score,
            severity=record.severity,
            priority=record.priority,
            call_vet=bool(record.call_vet),
            vet_gate=record.vet_gate,
            score_breakdown=record.score_breakdown,
            visual_evidence_as_tags=record.visual_evidence_as_tags,
            possible_conditions=data_meta.get("possible_conditions"),
            recommended_actions=data_meta.get("recommended_actions"),
            call_vet_if=data_meta.get("call_vet_if"),
            re_scan_window=data_meta.get("re_scan_window"),
            milk_withdrawal_risk=data_meta.get("milk_withdrawal_risk"),
            premium=record.premium_meta_json,
            pr=record.pr or {},
            reasoning=record.reasoning,
            model=record.model,
        )

    async def get_reports(
        self,
        *,
        user_id: str,
        cow_id: Optional[str] = None,
        scan_type: ScanType = "all",
        limit: int = 50,
        offset: int = 0,
    ) -> ReportsListResponse:
        # Normalize scan_type
        scan_type_normalized = (scan_type or "all").lower().strip()

        async with self.session_factory() as session:
            # 1. Fetch Disease Reports if requested
            disease_records = []
            disease_count = 0
            if scan_type_normalized in ("all", "disease_diagnosis", "disease"):
                query = db.select(DiseaseReportV1Schema).where(
                    DiseaseReportV1Schema.user_id == user_id,
                    DiseaseReportV1Schema.deleted_at.is_(None),
                )
                count_query = db.select(db.func.count(DiseaseReportV1Schema.uid)).where(
                    DiseaseReportV1Schema.user_id == user_id,
                    DiseaseReportV1Schema.deleted_at.is_(None),
                )
                if cow_id:
                    query = query.where(DiseaseReportV1Schema.cow_id == cow_id)
                    count_query = count_query.where(DiseaseReportV1Schema.cow_id == cow_id)

                count_res = await session.execute(count_query)
                disease_count = count_res.scalar() or 0

                if scan_type_normalized != "all":
                    query = query.order_by(DiseaseReportV1Schema.created_at.desc()).offset(offset).limit(limit)
                else:
                    # When 'all', fetch up to offset + limit to merge in memory accurately
                    query = query.order_by(DiseaseReportV1Schema.created_at.desc()).limit(offset + limit)

                res = await session.execute(query)
                disease_records = list(res.scalars().all())

            # 2. Fetch Manure / Cow Dung Reports if requested
            manure_records = []
            manure_count = 0
            if scan_type_normalized in ("all", "cow_dung", "manure"):
                query = db.select(ManureReportV1Schema).where(
                    ManureReportV1Schema.user_id == user_id,
                    ManureReportV1Schema.deleted_at.is_(None),
                )
                count_query = db.select(db.func.count(ManureReportV1Schema.uid)).where(
                    ManureReportV1Schema.user_id == user_id,
                    ManureReportV1Schema.deleted_at.is_(None),
                )
                if cow_id:
                    query = query.where(ManureReportV1Schema.cow_id == cow_id)
                    count_query = count_query.where(ManureReportV1Schema.cow_id == cow_id)

                count_res = await session.execute(count_query)
                manure_count = count_res.scalar() or 0

                if scan_type_normalized != "all":
                    query = query.order_by(ManureReportV1Schema.created_at.desc()).offset(offset).limit(limit)
                else:
                    query = query.order_by(ManureReportV1Schema.created_at.desc()).limit(offset + limit)

                res = await session.execute(query)
                manure_records = list(res.scalars().all())

            # 3. Format and Merge
            formatted_disease = [self._format_disease_report(r) for r in disease_records]
            formatted_manure = [self._format_manure_report(r) for r in manure_records]

            if scan_type_normalized in ("disease_diagnosis", "disease"):
                return ReportsListResponse(
                    total=disease_count,
                    limit=limit,
                    offset=offset,
                    items=formatted_disease,
                )
            elif scan_type_normalized in ("cow_dung", "manure"):
                return ReportsListResponse(
                    total=manure_count,
                    limit=limit,
                    offset=offset,
                    items=formatted_manure,
                )
            else:
                # Merge both lists and sort descending by created_at
                all_items = formatted_disease + formatted_manure
                all_items.sort(key=lambda x: x.created_at, reverse=True)
                paged_items = all_items[offset : offset + limit]

                return ReportsListResponse(
                    total=disease_count + manure_count,
                    limit=limit,
                    offset=offset,
                    items=paged_items,
                )

    async def get_report_by_id(
        self,
        *,
        report_id: str,
        user_id: str,
    ) -> Optional[Union[UnifiedDiseaseReportItem, UnifiedManureReportItem]]:
        async with self.session_factory() as session:
            # Check disease reports
            d_query = db.select(DiseaseReportV1Schema).where(
                DiseaseReportV1Schema.uid == report_id,
                DiseaseReportV1Schema.user_id == user_id,
                DiseaseReportV1Schema.deleted_at.is_(None),
            )
            d_res = await session.execute(d_query)
            d_record = d_res.scalar_one_or_none()
            if d_record is not None:
                return self._format_disease_report(d_record)

            # Check manure reports
            m_query = db.select(ManureReportV1Schema).where(
                ManureReportV1Schema.uid == report_id,
                ManureReportV1Schema.user_id == user_id,
                ManureReportV1Schema.deleted_at.is_(None),
            )
            m_res = await session.execute(m_query)
            m_record = m_res.scalar_one_or_none()
            if m_record is not None:
                return self._format_manure_report(m_record)

            return None


report_service = ReportService()

__all__ = ["ReportService", "report_service"]
