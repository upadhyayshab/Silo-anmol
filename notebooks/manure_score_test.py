"""
Standalone test: predict the cow-dung score (1-5) for local images using Gemini
directly (google-genai SDK), matching vlm-detectors' ManureScore scale.

Not wired into the app yet — this is the "first test with an image" step.
Run: python notebooks/manure_score_test.py <image1> <image2> ...
"""
import base64
import json
import mimetypes
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env", override=True)

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

MODEL = "gemini-3.1-pro-preview"  # same model vlm-detectors uses

# Deterministic lookups — identical to vlm-detectors' app/manure/models.py.
# Never model-generated; the model only predicts manure_score.
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
fixed 1-5 fecal scoring scale. Look only at the dung itself (consistency, form, moisture, \
fibre content) and pick exactly one score.

Score 1 — Watery Diarrhea (Severe Illness): liquid, watery, splattered, no form at all.
Score 2 — Loose Dung (Digestive Imbalance): soft, porridge-like, spreads out, lacks structure \
but not fully liquid.
Score 3 — Semi-solid (Healthy): dome-shaped pat with visible concentric rings, holds its own \
shape without being hard. This is the healthy target.
Score 4 — Thick Dung (Slow Digestion): firm, stacked/segmented pats, drier, visible fibre.
Score 5 — Hard Pellets (Bleeding While Passing Dung): hard, dry, separated ball-shaped pellets.

Return the single best-matching score and a one-sentence reason citing the visual evidence."""


class ManureScorePrediction(BaseModel):
    manure_score: int = Field(..., ge=1, le=5, description="The dung score 1-5.")
    reasoning: str = Field(..., description="One sentence citing the visual evidence for this score.")


def score_image(client: genai.Client, image_path: Path) -> dict:
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    image_bytes = image_path.read_bytes()

    response = client.models.generate_content(
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
            response_schema=ManureScorePrediction,
            temperature=0.1,
        ),
    )

    prediction: ManureScorePrediction = response.parsed
    score = prediction.manure_score

    return {
        "image_path": str(image_path),
        "model": MODEL,
        "manure_score": score,
        "health_status": SCORE_HEALTH_STATUS[score],
        "priority": SCORE_PRIORITY[score],
        "reasoning": prediction.reasoning,
    }


def main(image_paths: list[str]) -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    results = [score_image(client, Path(p)) for p in image_paths]
    print(json.dumps({"results": results}, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
