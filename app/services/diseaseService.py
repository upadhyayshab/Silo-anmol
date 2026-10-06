"""
Gemini-based cattle disease screening, driven by data/cow_disease_kb.json.

One Gemini call screens the image for every enabled disease at once (rather
than one call per disease, as vlm-detectors' /detect does), so adding a disease
adds prompt tokens, not requests. The response schema carries one required
slot per disease, so the model can neither skip nor duplicate one.

The model only judges what it sees (assessable, detected, confidence, severity,
signs, reasoning). Rules in _to_finding turn that into the final status, and
every farmer/vet-facing field (urgency, confirmatory action, questions, retake
hint) is a deterministic lookup on the KB row — never model-generated.
"""
import json
import re
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, create_model

from config import get_settings
from models.diseaseModels import Confidence, Severity
from utils.prompts import DISEASE_USER_PROMPT, build_disease_system_prompt

MODEL = "gemini-3.1-pro-preview"
TEMPERATURE = 0.0
THINKING_LEVEL = types.ThinkingLevel.HIGH

# KB rows screened by /detect/disease. Add an id here to enable another disease.
ENABLED_DISEASE_IDS = ("D01", "D02", "D03", "D04")

_KB_PATH = Path(__file__).parents[1] / "data" / "cow_disease_kb.json"
with open(_KB_PATH, encoding="utf-8") as _f:
    DISEASE_KB: list[dict] = json.load(_f)

_KB_BY_ID: dict[str, dict] = {row["disease_id"]: row for row in DISEASE_KB}
ENABLED_DISEASES: list[dict] = [_KB_BY_ID[disease_id] for disease_id in ENABLED_DISEASE_IDS]

SYSTEM_PROMPT = build_disease_system_prompt(ENABLED_DISEASES)


class _DiseaseAssessment(BaseModel):
    # Evidence fields come first so the model commits to what it saw before it
    # commits to a verdict. key_region_observation in particular pins down the
    # one reading (e.g. is the left fossa sunken or bulging) the verdict hinges on.
    key_region_observation: str = Field(
        ...,
        description=(
            "Neutral description of this disease's regions to inspect as they appear in the image — "
            "shape, contour, position relative to anatomical landmarks (e.g. backbone line), symmetry. "
            "Describe only; do not name the disease or give a verdict here."
        ),
    )
    signs_observed: list[str] = Field(..., description="Signs of this disease actually visible. Empty if none.")
    signs_against: list[str] = Field(..., description="Visible signs that argue against this disease. Empty if none.")
    assessable: bool = Field(..., description="False if the required view/regions are not visible enough to judge.")
    detected: bool = Field(..., description="True only if the minimum evidence to report is visible.")
    confidence: Confidence = Field(..., description="Per this disease's confidence rules.")
    severity: Severity = Field(..., description="Healthy unless detected.")
    reasoning: str = Field(..., description="One-to-two sentence justification citing the visual evidence.")


_DiseaseScreening = create_model(
    "_DiseaseScreening",
    is_cattle_image=(bool, Field(..., description="True if this is a real photo of a cow or buffalo.")),
    image_view=(str, Field(..., description="View shown, e.g. 'left side', 'right side', 'face front', 'rear', 'udder close-up'.")),
    **{
        row["disease_id"]: (_DiseaseAssessment, Field(..., description=f"Assessment for {row['disease_id']} — {row['disease_name']}."))
        for row in ENABLED_DISEASES
    },
    __base__=BaseModel,
)


def _farmer_questions(row: dict) -> list[str]:
    return [q.strip() + "?" for q in re.split(r"\?", row["farmer_question"]) if q.strip()]


def _to_finding(row: dict, assessment: _DiseaseAssessment | None) -> dict:
    """Apply the server-side rules to one model assessment. ``None`` means the
    image was not a cattle photo, so nothing about it is assessable."""
    base = {
        "disease_id": row["disease_id"],
        "disease_name": row["disease_name"],
        "severity_and_urgency": row["severity_and_urgency"],
        "confirmatory_action": row["confirmatory_action"],
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
            "retake_hint": row["image_quality_needed"],
        }

    evidence = {
        "signs_observed": assessment.signs_observed,
        "signs_against": assessment.signs_against,
        "reasoning": assessment.reasoning,
    }

    if not assessment.detected:
        return {**base, **evidence, "status": "not_detected", "confidence": None, "severity": "Healthy"}

    # LOW confidence is reported as suspected, never as a diagnosis. A detection
    # graded Healthy contradicts itself; the lowest real grade is used instead.
    return {
        **base,
        **evidence,
        "status": "suspected" if assessment.confidence == "LOW" else "detected",
        "confidence": assessment.confidence,
        "severity": "Mild" if assessment.severity == "Healthy" else assessment.severity,
    }


class DiseaseService:
    def __init__(self):
        settings = get_settings()
        self._client = genai.Client(api_key=settings.gemini_api_key)

    async def detect(self, image_bytes: bytes, mime_type: str) -> dict:
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
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=_DiseaseScreening,
                temperature=TEMPERATURE,
                thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL),
            ),
        )
        screening = response.parsed
        if screening is None:
            raise ValueError(f"Gemini returned no parseable screening: {response.text!r}")

        findings = [
            _to_finding(row, getattr(screening, row["disease_id"]) if screening.is_cattle_image else None)
            for row in ENABLED_DISEASES
        ]
        return {
            "is_cattle_image": screening.is_cattle_image,
            "image_view": screening.image_view,
            "findings": findings,
            "model": MODEL,
        }


__all__ = ["DiseaseService", "DISEASE_KB", "ENABLED_DISEASE_IDS"]
