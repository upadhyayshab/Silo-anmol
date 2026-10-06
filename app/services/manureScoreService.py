"""
Gemini-based cow-dung scoring service.

Predicts the 1-5 dung score directly from an image with a single Gemini call
(no chat/tool-calling). Same model and scale as vlm-detectors
(app/manure/models.py's ManureScore enum). health_status/priority are
deterministic lookups, kept in code (not fetched from Langfuse) since this
service doesn't have that dependency — only manure_score is model output.
"""
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from config import get_settings

MODEL = "gemini-3.8-flash"

SCORE_HEALTH_STATUS = {
    1: "Severe Illness",
    2: "Digestive Imbalance",
    3: "Healthy",
    4: "Slow Digestion",
    5: "Bleeding While Passing Dung",
}
SCORE_PRIORITY = {
    1: "Emergency",
    2: "Moderate",
    3: "Normal",
    4: "Moderate",
    5: "High",
}

SYSTEM_PROMPT = """You are a veterinary expert scoring Indian cattle dung from a photo, using a \
fixed 1-5 fecal scoring scale (reference: the standard 5-point manure scoring chart used in dairy \
herd management). Look only at the dung itself — height/mound shape, surface texture, glossiness, \
edge definition, presence of rings or ridges — and pick exactly one score.

Score 1 — Liquid / Watery (Severe Illness): No mound at all. Spreads flat and wide as a splattered \
puddle with indistinct, feathered edges that blend into the surrounding wet ground. Glossy wet sheen \
across the whole surface, no height or structure.

Score 2 — Loose Paste (Digestive Imbalance): Still runny and poorly contained, sitting in visible \
standing liquid/mud at its base, but with slightly more height than Score 1 — a shallow, ill-defined \
mound rather than a pure puddle. Glossy wet surface, no ring or dome structure, edges still blur into \
the wetness around it.

Score 3 — Optimal Porridge (Healthy, the target score): A well-defined dome-shaped mound that holds \
its own shape. Its surface is otherwise SMOOTH, broken by only ONE shallow, clean concentric ring or \
spiral groove near the centre (like a soft coil) — most of the mound is unbroken smooth surface, \
moist but not runny. Edges are clearly defined against the ground. This is the gold-standard, healthy \
appearance.

Score 4 — Stiff Stacked Mound (Slow Digestion): Taller and more upright than Score 3, with a rougher, \
coarser, less glossy surface and little to no visible ring pattern — instead showing stacked or \
layered texture. Firm, drier-looking, with distinct firm edges.

Score 5 — Dry Pelleted / Segmented (Severe dehydration or GI distress): The surface is covered EDGE \
TO EDGE in many deep, irregular, overlapping folds, wrinkles and fissures — like corrugated tripe or \
a folded brain — radiating chaotically across the whole mound, not one clean ring on an otherwise \
smooth surface. The key signal is the DENSITY and CHAOS of the ridging (many overlapping folds \
everywhere), not necessarily dryness — the surface can still be moist/glossy within the fold valleys \
if sitting in mud. Often segmented into stacked, wrinkled clumps rather than one smooth dome. \
Distinguish from Score 3 by fold count: Score 3 has ONE smooth ring on an otherwise plain surface; \
Score 5 has folds covering the ENTIRE surface with no smooth area left.

Return the single best-matching score and a one-sentence reason citing the visual evidence (height, \
surface texture, glossiness, ring pattern or lack of it, edge definition)."""


class _ManureScorePrediction(BaseModel):
    manure_score: int = Field(..., ge=1, le=5)
    reasoning: str


class ManureScoreService:
    def __init__(self):
        settings = get_settings()
        self._client = genai.Client(api_key=settings.gemini_api_key)

    async def score(self, image_bytes: bytes, mime_type: str) -> dict:
        response = await self._client.aio.models.generate_content(
            model=MODEL,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                        types.Part.from_text(text="Score this cow dung image."),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=_ManureScorePrediction,
                temperature=0.1,
            ),
        )
        prediction: _ManureScorePrediction = response.parsed
        manure_score = prediction.manure_score
        return {
            "manure_score": manure_score,
            "health_status": SCORE_HEALTH_STATUS[manure_score],
            "priority": SCORE_PRIORITY[manure_score],
            "reasoning": prediction.reasoning,
        }


__all__ = ["ManureScoreService"]
