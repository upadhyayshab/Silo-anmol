"""
Disease report service, screened with Gemini against the disease_kb.

Screens the photo for enabled cattle diseases, stores the diagnostic record
in disease_reports_v1 and returns the comprehensive farmer/vet-facing report body.
422 is raised when the photo does not clearly show cattle.
"""
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, create_model

from config import get_engine, get_settings
from managers import DiseaseReportV1Manager, DiseaseReportV1Schema
from models.diseaseModels import Confidence, Severity
from services.diseaseKbService import disease_kb_service
from utils.prompts import DISEASE_USER_PROMPT, build_disease_system_prompt

MODEL = "gemini-3.1-pro-preview"
TEMPERATURE = 0.0
THINKING_LEVEL = types.ThinkingLevel.HIGH

# All 31 diseases (D01 to D31) from disease_kb
ENABLED_DISEASE_IDS = tuple(f"D{i:02d}" for i in range(1, 32))

# Fallback JSON path for offline/unit tests
_KB_PATH = Path(__file__).parents[1] / "data" / "cow_disease_kb_multilingual.json"
if not _KB_PATH.exists():
    _KB_PATH = Path(__file__).parents[1] / "data" / "cow_disease_kb.json"

with open(_KB_PATH, encoding="utf-8") as _f:
    _FALLBACK_KB: list[dict] = json.load(_f)

_FALLBACK_BY_LANG_AND_ID: dict[tuple[str, str], dict] = {
    (row.get("lang_code", "en"), row["disease_id"]): row for row in _FALLBACK_KB
}
_FALLBACK_BY_ID: dict[str, dict] = {
    row["disease_id"]: row for row in _FALLBACK_KB if row.get("lang_code", "en") == "en"
}


class _DiseaseAssessment(BaseModel):
    disease_id: str = Field(..., description="Disease ID, e.g. 'D01', 'D02', ..., 'D31'")
    key_region_observation: str = Field(
        ...,
        description=(
            "Neutral description of this disease's regions to inspect as they appear in the image — "
            "shape, contour, position relative to anatomical landmarks (e.g. backbone line), symmetry. "
            "Describe only; do not name the disease or give a verdict here."
        ),
    )
    signs_observed: list[str] = Field(default_factory=list, description="Signs of this disease actually visible. Empty if none.")
    signs_against: list[str] = Field(default_factory=list, description="Visible signs that argue against this disease. Empty if none.")
    assessable: bool = Field(..., description="False if the required view/regions are not visible enough to judge.")
    detected: bool = Field(..., description="True only if the minimum evidence to report is visible.")
    confidence: Optional[Confidence] = Field(None, description="Per this disease's confidence rules.")
    severity: Severity = Field("Healthy", description="Healthy unless detected.")
    reasoning: str = Field(..., description="One-to-two sentence justification citing the visual evidence.")


class _DiseaseScreening(BaseModel):
    is_cattle_image: bool = Field(..., description="True if this is a real photo of a cow or buffalo.")
    image_view: str = Field(..., description="View shown, e.g. 'left side', 'right side', 'face front', 'rear', 'udder close-up'.")
    assessments: list[_DiseaseAssessment] = Field(..., description="Assessment for each of the 31 listed diseases in order (D01 to D31).")


def _build_screening_schema(enabled_rows: list[dict]) -> type[BaseModel]:
    return _DiseaseScreening


def _farmer_questions(row: dict) -> list[str]:
    q_text = row.get("farmer_question") or ""
    return [q.strip() + "?" for q in re.split(r"\?", q_text) if q.strip()]


def _to_finding(row: dict, assessment: _DiseaseAssessment | None) -> dict:
    base = {
        "disease_id": row["disease_id"],
        "disease_name": row["disease_name"],
        "severity_and_urgency": row.get("severity_and_urgency") or "",
        "confirmatory_action": row.get("confirmatory_action") or "",
        "farmer_questions": _farmer_questions(row),
        "retake_hint": None,
    }

    if assessment is None or not assessment.assessable:
        return {
            **base,
            "status": "not_assessable",
            "confidence": None,
            "severity": "Healthy",
            "signs_observed": [],
            "signs_against": [],
            "reasoning": assessment.reasoning if assessment else "The image is not a photo of cattle.",
            "retake_hint": row.get("image_quality_needed"),
        }

    evidence = {
        "signs_observed": assessment.signs_observed,
        "signs_against": assessment.signs_against,
        "reasoning": assessment.reasoning,
    }

    if not assessment.detected:
        return {**base, **evidence, "status": "not_detected", "confidence": None, "severity": "Healthy"}

    return {
        **base,
        **evidence,
        "status": "suspected" if assessment.confidence == "LOW" else "detected",
        "confidence": assessment.confidence,
        "severity": "Mild" if assessment.severity == "Healthy" else assessment.severity,
    }


class DiseaseReportService:
    def __init__(self):
        settings = get_settings()
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._reports = DiseaseReportV1Manager(get_engine(settings.name))

    async def _get_enabled_diseases(self, lang_code: str = "en") -> list[dict]:
        try:
            db_rows = await disease_kb_service.classification_rows()
            if db_rows:
                by_id = {row.disease_id: row for row in db_rows}
                out = []
                for did in ENABLED_DISEASE_IDS:
                    r = by_id.get(did)
                    if r:
                        out.append({
                            "disease_id": r.disease_id,
                            "disease_name": r.disease_name,
                            "also_known_as": r.also_known_as,
                            "required_image_view": r.required_image_view,
                            "detectable_from_side_image": r.detectable_from_side_image,
                            "visual_detectability": r.visual_detectability,
                            "regions_to_inspect": r.regions_to_inspect,
                            "key_visual_signs": r.key_visual_signs,
                            "supporting_visual_signs": r.supporting_visual_signs,
                            "posture_gait_behaviour_cues": r.posture_gait_behaviour_cues,
                            "look_alikes_and_how_to_differentiate": r.look_alikes_and_how_to_differentiate,
                            "signs_that_argue_against": r.signs_that_argue_against,
                            "minimum_evidence_to_report": r.minimum_evidence_to_report,
                            "confidence_rules": r.confidence_rules,
                            "severity_and_urgency": r.severity_and_urgency,
                            "risk_context": r.risk_context,
                            "farmer_question": r.farmer_question,
                            "image_quality_needed": r.image_quality_needed,
                            "confirmatory_action": r.confirmatory_action,
                            "notes_for_model": r.notes_for_model,
                            "pr": r.pr or {},
                        })
                if out:
                    return out
        except Exception:
            pass
        return [_FALLBACK_BY_ID[did] for did in ENABLED_DISEASE_IDS if did in _FALLBACK_BY_ID]

    async def create_report(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        img_url: str,
        lang_code: str,
        user_id: str,
        cow_id: str | None,
        subs_id: str | None,
        has_subs: bool = True,
    ) -> dict:
        enabled_diseases = await self._get_enabled_diseases(lang_code)
        if not enabled_diseases:
            raise HTTPException(status_code=503, detail="No diseases configured for screening")

        system_prompt = build_disease_system_prompt(enabled_diseases)
        screening_schema = _build_screening_schema(enabled_diseases)

        started = time.monotonic()
        response = await self._client.aio.models.generate_content(
            model=MODEL,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                        types.Part.from_text(text=DISEASE_USER_PROMPT),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=screening_schema,
                temperature=TEMPERATURE,
                thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL),
            ),
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        screening = response.parsed
        if screening is None:
            raise ValueError(f"Gemini returned no parseable screening: {response.text!r}")

        if not screening.is_cattle_image:
            raise HTTPException(
                status_code=422,
                detail={"code": "NOT_CATTLE", "message": "Image is not a photo of a cow or buffalo."},
            )

        assessments_by_id = {a.disease_id: a for a in (screening.assessments or [])}
        findings = []
        for did in ENABLED_DISEASE_IDS:
            assessment = assessments_by_id.get(did)
            loc_schema = await disease_kb_service.get_by_id(did, lang_code=lang_code)
            if loc_schema:
                row_dict = {
                    "disease_id": loc_schema.disease_id,
                    "disease_name": loc_schema.disease_name,
                    "severity_and_urgency": loc_schema.severity_and_urgency,
                    "confirmatory_action": loc_schema.confirmatory_action,
                    "farmer_question": loc_schema.farmer_question,
                    "image_quality_needed": loc_schema.image_quality_needed,
                    "also_known_as": loc_schema.also_known_as,
                }
            else:
                row_dict = (
                    _FALLBACK_BY_LANG_AND_ID.get((lang_code, did))
                    or _FALLBACK_BY_ID.get(did)
                    or {"disease_id": did, "disease_name": did}
                )
            findings.append(_to_finding(row_dict, assessment))

        detected_count = sum(1 for f in findings if f["status"] == "detected")
        suspected_count = sum(1 for f in findings if f["status"] == "suspected")

        # Top detected or suspected finding
        positive_findings = [f for f in findings if f["status"] in ("detected", "suspected")]
        primary_diagnosis = positive_findings[0]["disease_name"] if positive_findings else None

        # Call vet if any condition is detected with Moderate/Severe/Critical severity
        call_vet = any(
            f["severity"] in ("Moderate", "Severe", "Critical") or "emergency" in f["severity_and_urgency"].lower()
            for f in positive_findings
        )

        overall_reasoning = positive_findings[0]["reasoning"] if positive_findings else "No cattle diseases detected in visible regions."

        body = {
            "user_id": user_id,
            "cow_id": cow_id,
            "subs_id": subs_id,
            "has_subs": has_subs,
            "ui_version": "1.1" if has_subs else "1.0",
            "lang_code": lang_code,
            "img_url": img_url,
            "is_cattle_image": screening.is_cattle_image,
            "image_view": screening.image_view,
            "primary_diagnosis": primary_diagnosis,
            "detected_count": detected_count,
            "suspected_count": suspected_count,
            "call_vet": call_vet,
            "findings": findings,
            "reasoning": overall_reasoning,
            "model": MODEL,
            "pr": {},
        }

        record = await self._reports.create(
            DiseaseReportV1Schema(
                user_id=user_id,
                cow_id=cow_id,
                lang_code=lang_code,
                img_url=img_url,
                is_cattle_image=screening.is_cattle_image,
                image_view=screening.image_view,
                findings=findings,
                detected_count=detected_count,
                suspected_count=suspected_count,
                primary_diagnosis=primary_diagnosis,
                reasoning=overall_reasoning,
                model=MODEL,
                latency_ms=latency_ms,
            ),
            upstreamId=user_id,
        )

        response_dict = {
            "report_id": record.uid,
            "created_at": record.created_at,
            **body,
        }
        if cow_id is None:
            del response_dict["cow_id"]
        return response_dict


disease_report_service = DiseaseReportService()

__all__ = ["DiseaseReportService", "disease_report_service"]
