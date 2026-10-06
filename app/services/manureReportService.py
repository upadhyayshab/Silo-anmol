"""
Manure report, classified hierarchically in two Gemini calls:

  1. Score — quality gate (is_manure / is_fresh), visual-evidence tags and the
     1-5 consistency score, judged on shape alone. A failed gate stops here.
  2. Sub-score — the sub-code, chosen only among that score's manure_kb rows,
     so a Score 1 dung can never come back with a Score 2 sub-code. Skipped
     when the score has a single sub-code.

The farmer-facing body is then built from the matched KB entry and stored in
manure_reports_v1. Gemini supplies only the gate, tags, score and sub-code;
every other field is a KB lookup or a fixed score rule.
"""
import enum
import re
import time
from typing import Literal

from fastapi import HTTPException
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, create_model

from config import get_engine, get_settings
from managers import ManureKbSchema, ManureReportV1Manager, ManureReportV1Schema
from services.manureKbService import manure_kb_service
from services.manureScoreService import SCORE_HEALTH_STATUS, SCORE_PRIORITY
from services.manureScoreService import SYSTEM_PROMPT as _SCORER_PROMPT
from utils.prompts import (
    MANURE_EVIDENCE_TAGS,
    MANURE_SCORE_USER_PROMPT,
    MANURE_SUB_SCORE_USER_PROMPT,
    build_manure_score_system_prompt,
    build_manure_sub_score_system_prompt,
)

# Pinned to a versioned model, not an alias like gemini-flash-latest, so an
# upstream model swap cannot silently change scores. Chosen by
# docs/benchmarks/MANURE_MODEL_BENCHMARK.md: same accuracy as 3.1 Pro on
# testing_img/, ~1.5x faster and ~2.7x cheaper per scan.
MODEL = "gemini-3.8-flash"
TEMPERATURE = 0.0
# LOW thinking: on testing_img/ it was more accurate and more stable than the
# model's default thinking, as well as faster and cheaper.
SCORE_THINKING_LEVEL: types.ThinkingLevel | None = types.ThinkingLevel.LOW
SUB_SCORE_THINKING_LEVEL: types.ThinkingLevel | None = types.ThinkingLevel.LOW

UI_VERSION_FREE = "1.0"
UI_VERSION_PREMIUM = "1.1"
CALL_VET_SCORES = {1, 5}

# The Score 1-5 definitions from /detect/manure's scorer, without its own
# preamble and answer instructions.
SCORE_SCALE = _SCORER_PROMPT[_SCORER_PROMPT.index("Score 1 —"):_SCORER_PROMPT.index("Return the single")]
SCORE_SYSTEM_PROMPT = build_manure_score_system_prompt(SCORE_SCALE)

# Score-level text from the requirements sketch. A '<score>_FREE' manure_kb row
# overrides title / call_vet_if for its score once the KB owners add it.
SCORE_BREAKDOWN = {
    "1": "Extreme abnormality — call a vet",
    "2": "May turn severe — act now to bring it back to normal",
    "3": "Good manure — healthy",
    "4": "May turn severe — act now to bring it back to normal",
    "5": "Extreme abnormality — call a vet",
}
DEFAULT_CALL_VET_IF = (
    "Symptoms persist, the cow stops eating, or you notice blood, severe diarrhoea, or weakness."
)


class _EvidenceTag(BaseModel):
    tag: Literal[MANURE_EVIDENCE_TAGS]  # type: ignore[valid-type]
    value: str


class _ScorePrediction(BaseModel):
    # Gate and evidence come before the verdict so the model commits to what it
    # saw first.
    is_manure: bool
    is_fresh: bool
    qa_reason: str
    visual_evidence_as_tags: list[_EvidenceTag] = Field(..., min_length=1, max_length=4)
    score_reasoning: str
    manure_score: int = Field(..., ge=1, le=5)


def _sub_score_model(sub_codes: tuple[str, ...]) -> type[BaseModel]:
    """Stage-2 schema; sub_code is an enum of just this score's KB codes, so the
    model can neither invent one nor leave the score."""
    sub_code_enum = enum.Enum("SubCode", {code.replace("-", "_"): code for code in sub_codes})
    return create_model(
        "_SubScorePrediction",
        reasoning=(str, Field(...)),
        sub_code=(sub_code_enum, Field(...)),
        __base__=BaseModel,
    )


def _split_conditions(text: str | None) -> list[str]:
    if not text:
        return []
    return [part.strip().rstrip(".").strip() for part in text.split(" / ") if part.strip()]


def _split_actions(text: str | None) -> list[str]:
    if not text:
        return []
    return [sentence.strip() for sentence in re.split(r"(?<=[.!])\s+", text) if sentence.strip()]


class ManureReportService:
    def __init__(self):
        self._client = genai.Client(api_key=get_settings().gemini_api_key)
        self._reports = ManureReportV1Manager(get_engine(get_settings().name))
        # Per-score stage-2 prompt + schema, keyed on the score's KB rows so a
        # KB cache refresh rebuilds them.
        self._sub_score_cache: dict[int, tuple[tuple[str, ...], str, type[BaseModel]]] = {}

    async def _generate(self, image_bytes: bytes, mime_type: str, system_prompt: str, user_prompt: str,
                        schema: type[BaseModel], thinking_level: types.ThinkingLevel | None = None):
        response = await self._client.aio.models.generate_content(
            model=MODEL,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                        types.Part.from_text(text=user_prompt),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=TEMPERATURE,
                thinking_config=types.ThinkingConfig(thinking_level=thinking_level) if thinking_level else None,
            ),
        )
        if response.parsed is None:
            raise ValueError(f"Gemini returned no parseable answer: {response.text!r}")
        return response.parsed

    async def _score(self, image_bytes: bytes, mime_type: str, lang_code: str) -> _ScorePrediction:
        return await self._generate(
            image_bytes, mime_type, SCORE_SYSTEM_PROMPT.replace("{lang_code}", lang_code),
            MANURE_SCORE_USER_PROMPT, _ScorePrediction, SCORE_THINKING_LEVEL,
        )

    async def _sub_score(self, image_bytes: bytes, mime_type: str, score: int,
                         candidates: list[ManureKbSchema]) -> tuple[ManureKbSchema, str | None]:
        """The matched sub-code row within ``score`` and the model's reasoning
        (None when the score has a single sub-code and no call was needed)."""
        if len(candidates) == 1:
            return candidates[0], None
        key = tuple(f"{row.uid}:{row.updated_at}" for row in candidates)
        cached = self._sub_score_cache.get(score)
        if cached is None or cached[0] != key:
            cached = (
                key,
                build_manure_sub_score_system_prompt(score, candidates[0].consistency_class, candidates),
                _sub_score_model(tuple(row.sub_code for row in candidates)),
            )
            self._sub_score_cache[score] = cached
        prediction = await self._generate(image_bytes, mime_type, cached[1], MANURE_SUB_SCORE_USER_PROMPT, cached[2],
                                          SUB_SCORE_THINKING_LEVEL)
        sub_code = prediction.sub_code.value
        return next(row for row in candidates if row.sub_code == sub_code), prediction.reasoning

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
        has_subs: bool,
    ) -> dict:
        rows = await manure_kb_service.classification_rows()
        if not rows:
            raise HTTPException(status_code=503, detail="Manure knowledge base is empty")

        started = time.monotonic()
        scored = await self._score(image_bytes, mime_type, lang_code)
        if not scored.is_manure:
            raise HTTPException(status_code=422, detail={"code": "NOT_MANURE", "message": scored.qa_reason})
        if not scored.is_fresh:
            raise HTTPException(status_code=422, detail={"code": "NOT_FRESH", "message": scored.qa_reason})

        score = int(scored.manure_score)
        candidates = [row for row in rows if int(row.score) == score]
        if not candidates:
            raise HTTPException(status_code=503, detail=f"Manure knowledge base has no sub-codes for score {score}")
        matched, sub_reasoning = await self._sub_score(image_bytes, mime_type, score, candidates)
        sub_code = matched.sub_code
        latency_ms = int((time.monotonic() - started) * 1000)
        reasoning = scored.score_reasoning if sub_reasoning is None else f"{scored.score_reasoning} {sub_reasoning}"

        # Premium reads the matched sub-code entry; free reads the score-level
        # entry when the KB has one, else falls back to the sub-code entry.
        body_row = matched if has_subs else (await manure_kb_service.free_row(score, lang_code) or matched)
        body_row = await manure_kb_service.localized(body_row, lang_code)
        free_row = await manure_kb_service.free_row(score, lang_code)

        tags = [tag.model_dump() for tag in scored.visual_evidence_as_tags]
        premium = None
        if has_subs:
            premium = {
                "manure_sub_score": sub_code,
                "sub_type_name": body_row.sub_type_name,
                "root_cause_etiology": body_row.root_cause_etiology,
                "on_farm_confirmatory_check": body_row.on_farm_confirmatory_check,
                "reasoning": reasoning,
            }

        body = {
            "user_id": user_id,
            "cow_id": cow_id,
            "subs_id": subs_id,
            "has_subs": has_subs,
            "ui_version": UI_VERSION_PREMIUM if has_subs else UI_VERSION_FREE,
            "lang_code": lang_code,
            "kb_reference_id": body_row.kb_uid,
            "img_url": img_url,
            "manure_score": score,
            "title": (free_row.sub_type_name if free_row and free_row.sub_type_name else SCORE_HEALTH_STATUS[score]),
            "severity": matched.severity,
            "priority": SCORE_PRIORITY[score],
            "call_vet": score in CALL_VET_SCORES,
            "vet_gate": matched.vet_gate,
            "score_breakdown": SCORE_BREAKDOWN,
            "possible_conditions": _split_conditions(body_row.diagnosed_clinical_condition),
            "visual_evidence_as_tags": tags,
            "recommended_actions": _split_actions(body_row.actionable_advice),
            "call_vet_if": body_row.call_vet_if or (free_row.call_vet_if if free_row else None) or DEFAULT_CALL_VET_IF,
            "re_scan_window": body_row.re_scan_window,
            "milk_withdrawal_risk": body_row.milk_withdrawal_risk,
            "premium": premium,
            "pr": body_row.pr or {},
        }

        record = await self._reports.create(
            ManureReportV1Schema(
                user_id=user_id,
                cow_id=cow_id,
                subs_id=subs_id,
                has_subs=has_subs,
                ui_version=body["ui_version"],
                lang_code=lang_code,
                img_url=img_url,
                manure_score=score,
                manure_sub_score=sub_code,
                kb_reference_id=body["kb_reference_id"],
                title=body["title"],
                severity=body["severity"],
                priority=body["priority"],
                call_vet=body["call_vet"],
                vet_gate=body["vet_gate"],
                score_breakdown=SCORE_BREAKDOWN,
                visual_evidence_as_tags=tags,
                data_meta_json={k: v for k, v in body.items() if k != "premium"},
                premium_meta_json=premium,
                pr=body["pr"],
                reasoning=reasoning,
                model=MODEL,
                latency_ms=latency_ms,
            ),
            upstreamId=user_id,
        )

        response = {"report_id": record.uid, "created_at": record.created_at, **body}
        if cow_id is None:
            del response["cow_id"]
        return response


__all__ = ["ManureReportService"]
