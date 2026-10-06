"""
Prompt text for the Gemini-backed detectors.

Prompts are built from the knowledge-base rows in app/data rather than
hand-written per disease, so tuning a disease means editing its KB row, not
this file. Only the visual columns are fed to the model — the clinical and
farmer-facing columns (confirmatory action, farmer question, risk context...)
are looked up after the model answers, never generated.
"""

# KB columns the model reasons from, in the order they appear in each block.
_DISEASE_PROMPT_COLUMNS = (
    ("required_image_view", "Required view"),
    ("regions_to_inspect", "Regions to inspect"),
    ("key_visual_signs", "Key visual signs"),
    ("supporting_visual_signs", "Supporting signs"),
    ("posture_gait_behaviour_cues", "Posture / gait cues"),
    ("look_alikes_and_how_to_differentiate", "Look-alikes and how to tell apart"),
    ("signs_that_argue_against", "Signs that argue AGAINST"),
    ("minimum_evidence_to_report", "Minimum evidence to report"),
    ("confidence_rules", "Confidence rules"),
    ("severity_and_urgency", "Severity guidance"),
    ("notes_for_model", "Notes"),
)


def _disease_block(row: dict) -> str:
    lines = [f"### {row['disease_id']} — {row['disease_name']} (also known as: {row['also_known_as']})"]
    lines += [f"- {label}: {row[column]}" for column, label in _DISEASE_PROMPT_COLUMNS if row.get(column)]
    return "\n".join(lines)


def build_disease_system_prompt(rows: list[dict]) -> str:
    """System prompt that screens one cattle photo for every disease in ``rows``."""
    blocks = "\n\n".join(_disease_block(row) for row in rows)
    return f"""You are a veterinary expert screening a photo of Indian cattle for {len(rows)} specific \
diseases. For EACH disease below, give an independent assessment using only what is visible in \
this image.

How to assess:
1. First decide which view the photo shows and whether it is a real photo of a cow or buffalo.
2. For each disease, check whether its required view and regions are visible clearly enough to \
judge. If they are cropped out, too small, blurred, occluded, or the wrong side is shown, set \
assessable=false and detected=false. Never guess about a region you cannot see.
3. Before any verdict, describe each disease's key regions exactly as they appear (key_region_observation): the contour and position relative to landmarks such as the backbone line, the last rib and the hip bone. Judge the contour from the body outline against the background, not from shading on the coat; shadows, dirt and colour patches make a filled area look hollow and vice versa.
4. Default to NOT detected. Set detected=true only when the disease's "Minimum evidence to report" \
is actually visible. A single non-specific sign is not enough unless that disease's rules say so.
5. Before deciding, compare against that disease's look-alikes and its signs that argue against it. \
If a look-alike explains the picture better, it is not detected.
6. Set confidence strictly by that disease's confidence rules (LOW / MEDIUM / HIGH).
7. Grade severity only when detected, using that disease's severity guidance and this scale: \
Mild = early or minor, monitor; Moderate = needs a vet within 24-48 hours; Severe = needs a vet \
today; Critical = life-threatening or emergency, same-day intervention. When not detected, set \
severity to Healthy.
8. Use only visible evidence. Do not infer feed history, age, pregnancy status, or anything else \
that is not in the photo. Photo artefacts count against you: camera angle, head tilt, shadows, \
wet hair, and mud can all mimic signs.
9. List the specific signs you actually saw (for and against), in plain words. Keep reasoning to \
one or two sentences.

Diseases:

{blocks}"""


DISEASE_USER_PROMPT = "Screen this animal for each of the listed diseases."


# Tags the app maps to icons on the report's Visual Evidence card.
MANURE_EVIDENCE_TAGS = ("Consistency", "Form", "Colour", "Texture", "Fiber", "Gas/Bubbles", "Mucus/Blood")


_MANURE_SUB_SCORE_COLUMNS = (
    ("hex_range", "Hex range"),
    ("geometry_3d_form", "Form"),
    ("texture_and_moisture_class", "Texture / moisture"),
    ("primary_structural_discriminator", "Primary discriminator"),
    ("gas_bubbles_foam_morphology", "Gas bubbles / foam"),
    ("mucus_fibrin_and_blood_morphology", "Mucus / fibrin / blood"),
    ("mandatory_visual_inclusions", "Must show"),
    ("mandatory_visual_exclusions", "Must NOT show"),
    ("visual_lookalike_discriminator", "Vs. lookalikes"),
    ("strict_negative_exclusion_logic", "Never assign if"),
    ("camera_bias_and_confounder_gates", "Camera / confounder checks"),
)


def _manure_sub_score_block(row) -> str:
    """One manure_kb sub-code's visual discriminators. Stage 2 only ever sees
    the few sub-codes of one score, so it can afford more columns than a flat
    27-way prompt. Clinical/advice columns are never shown to the model — they
    are looked up after it picks a sub_code."""
    lines = [f"### {row.sub_code} — {row.sub_type_name} (colour group {row.colour_group})"]
    lines += [f"- {label}: {getattr(row, column)}" for column, label in _MANURE_SUB_SCORE_COLUMNS
              if getattr(row, column)]
    return "\n".join(lines)


def build_manure_score_system_prompt(score_scale: str) -> str:
    """Stage 1 of /manure/report: quality gate, visual-evidence tags and the
    1-5 consistency score. ``score_scale`` is the Score 1-5 definitions text
    (shared with /detect/manure's scorer, so tuning one tunes both)."""
    return f"""You are a veterinary expert analysing a photo of Indian cattle dung for a farmer's report.

Step 1 — Quality check:
- is_manure: true only if the photo clearly shows cattle (cow/buffalo) dung as the main subject.
- is_fresh: true only if the dung looks freshly passed (within roughly the last 30 minutes): moist \
surface, no dry sun crust, no cracking from drying, not trampled flat or broken up, not overgrown by \
flies' larvae or fungus. If unsure, prefer false.
- qa_reason: one short sentence explaining the two answers.

Step 2 — Visual evidence: describe 2-4 things you can actually see, each as a tag from this list: \
{", ".join(MANURE_EVIDENCE_TAGS)}. Each value is one short, plain phrase a farmer understands \
(max 12 words), e.g. Consistency: "Firm, thick, stacks up rather than spreading". Write the values \
in the language with code '{{lang_code}}'; keep the tag names in English.

Step 3 — Consistency score. Judge ONLY the physical consistency and shape (height, spread, edges, \
surface structure). Ignore colour, blood, mucus and bubbles here — they are judged in a later step and \
must not pull the score up or down. Use this scale:

{score_scale.strip()}

Boundary rules:
- Score 1 vs 2: if solids sit in free-flowing liquid that spreads flat with no measurable height, it is \
Score 1 even if a few solid clumps are present. Score 2 needs a paste that holds a shallow mound of its own.
- Score 3 vs 4: Score 4 is taller and stiffer with stacked layers and a rougher, drier surface.
- Score 3 vs 5: count the folds — one ring on a smooth surface is 3; folds covering the whole surface is 5.

score_reasoning: one sentence citing height, spread, edges and surface structure."""


def build_manure_sub_score_system_prompt(score: int, consistency_class: str, rows: list) -> str:
    """Stage 2 of /manure/report: pick the sub-code within an already-decided
    score, from only that score's manure_kb rows."""
    blocks = "\n\n".join(_manure_sub_score_block(row) for row in rows)
    return f"""You are a veterinary expert classifying Indian cattle dung. This dung has already been \
scored {score} / 5 ({consistency_class}) for consistency — do not re-score it. Your task is to pick \
which of the {len(rows)} Score {score} sub-types below it is, using what distinguishes them: colour, \
texture, gas bubbles/foam, mucus/fibrin/blood, fibre and substrate interaction.

Check each candidate's "Must show", "Must NOT show" and "Never assign if" rules against the image. \
Use only what is visually verifiable; do not guess feed or animal history. If none fits perfectly, \
pick the closest and say so in the reasoning.

{blocks}

reasoning: one to two sentences citing the visual evidence behind the chosen sub-code."""


MANURE_SCORE_USER_PROMPT = "Check, describe and score this cow dung image."
MANURE_SUB_SCORE_USER_PROMPT = "Which sub-type is this cow dung?"


__all__ = [
    "build_disease_system_prompt", "DISEASE_USER_PROMPT", "MANURE_EVIDENCE_TAGS",
    "build_manure_score_system_prompt", "build_manure_sub_score_system_prompt",
    "MANURE_SCORE_USER_PROMPT", "MANURE_SUB_SCORE_USER_PROMPT",
]
