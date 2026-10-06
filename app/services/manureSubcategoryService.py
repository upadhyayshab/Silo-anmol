"""
Sub-category (colour/condition) classification for cow dung, driven entirely
by data/manure_master_table.json (27 sub-codes across the 5 consistency
scores). One Gemini call classifies which sub-code the image matches, using
only the visual discriminator columns from the table; every other field
(diagnosis, severity, vet gate, advice...) is a deterministic lookup on the
matched row — never model-generated, so it's exactly what's in the table.

Independent of ManureScoreService (the plain 1-5 scorer): this endpoint
determines the score AND the sub-code together, from the sub-code table
itself (Score is a column on each row), in a single classification call.
"""
import enum
import json
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, create_model

from config import get_settings

MODEL = "gemini-3.8-flash"

_TABLE_PATH = Path(__file__).parents[1] / "data" / "manure_master_table.json"
with open(_TABLE_PATH, encoding="utf-8") as _f:
    MASTER_TABLE: list[dict] = json.load(_f)

_ROWS_BY_CODE: dict[str, dict] = {row["Sub_Code"]: row for row in MASTER_TABLE}

# Structured-output enum constraining the model to a real sub-code — built
# from the table so it can never invent a code that isn't in it.
SubCode = enum.Enum("SubCode", {row["Sub_Code"].replace("-", "_"): row["Sub_Code"] for row in MASTER_TABLE})


def _candidate_block(row: dict) -> str:
    """One sub-code's visual-only discriminators, for the classification prompt.
    Deliberately excludes the clinical/advice columns — those are looked up
    deterministically after the model picks a sub_code, never fed to it."""
    return (
        f"### {row['Sub_Code']} — {row['Sub_Type_Name']} (Score {row['Score']}, colour group {row['Colour_Group']})\n"
        f"- Hex range: {row['Hex_Range']}\n"
        f"- Form: {row['Geometry_3D_Form']}\n"
        f"- Primary discriminator: {row['Primary_Structural_Discriminator']}\n"
        f"- Must show: {row['Mandatory_Visual_Inclusions']}\n"
        f"- Must NOT show: {row['Mandatory_Visual_Exclusions']}\n"
        f"- Vs. lookalikes: {row['Visual_Lookalike_Discriminator']}\n"
    )


def _build_system_prompt() -> str:
    blocks = "\n".join(_candidate_block(row) for row in MASTER_TABLE)
    return f"""You are a veterinary expert classifying Indian cattle dung photos against a fixed \
27-way sub-code taxonomy. Each sub-code belongs to one of the 5 consistency scores (1-5) and \
represents a distinct colour/condition pattern. Pick exactly ONE sub-code that best matches the \
image, using only what is visually verifiable (colour, form, texture, gas bubbles, blood/mucus, \
substrate interaction). Do not guess feed history or animal history that isn't visible.

{blocks}

If the image is ambiguous, underexposed, or matches no sub-code's mandatory inclusions, still \
pick the closest match but say so plainly in your reasoning.

Return the single best-matching sub_code and a one-to-two sentence reason citing the specific \
visual evidence (colour, form, bubbles/mucus/blood if any) that led to it."""


SYSTEM_PROMPT = _build_system_prompt()

_SubCategoryPrediction = create_model(
    "_SubCategoryPrediction",
    sub_code=(SubCode, Field(..., description="The single best-matching sub-code from the taxonomy.")),
    reasoning=(str, Field(..., description="One-to-two sentence visual justification.")),
    __base__=BaseModel,
)


def build_common_message(row: dict) -> str:
    """The one shared, deterministic message composer — every sub-code's
    farmer-facing text is built by this same function from its own table
    row, never hand-written per code."""
    parts = [f"{row['Diagnosed_Clinical_Condition']} ({row['Severity']})."]
    if row["Actionable_Advice"]:
        parts.append(row["Actionable_Advice"])
    if row["Vet_Gate"] and row["Vet_Gate"] != "NO":
        parts.append(f"Vet consultation: {row['Vet_Gate']}.")
    if row["Re_Scan_Window"]:
        parts.append(f"Re-scan window: {row['Re_Scan_Window']}.")
    return " ".join(parts)


class ManureSubcategoryService:
    def __init__(self):
        settings = get_settings()
        self._client = genai.Client(api_key=settings.gemini_api_key)

    async def classify(self, image_bytes: bytes, mime_type: str) -> dict:
        response = await self._client.aio.models.generate_content(
            model=MODEL,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                        types.Part.from_text(text="Classify this cow dung image's sub-code."),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=_SubCategoryPrediction,
                temperature=0.1,
            ),
        )
        prediction = response.parsed
        sub_code = prediction.sub_code.value
        row = _ROWS_BY_CODE[sub_code]

        return {
            "score": int(row["Score"]),
            "sub_code": sub_code,
            "sub_type_name": row["Sub_Type_Name"],
            "colour_group": row["Colour_Group"],
            "diagnosed_clinical_condition": row["Diagnosed_Clinical_Condition"],
            "severity": row["Severity"],
            "vet_gate": row["Vet_Gate"],
            "message": build_common_message(row),
            "reasoning": prediction.reasoning,
        }


__all__ = ["ManureSubcategoryService", "MASTER_TABLE", "build_common_message"]
