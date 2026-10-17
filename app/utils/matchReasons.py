"""Plain-language reasons for a recommendation, built from the factor points
the scoring engine already saved with each result (0 = poor, 1 = acceptable,
2 = ideal). Shared by the wizard submit response and the report dossiers so
both screens explain a crop the same way."""
from typing import Optional

_SEASON_NAMES = {"KHARIF": "Kharif", "RABI": "Rabi", "ZAID": "Summer", "LONGTERM": "long-term planting"}

# factor -> {points: reason}; a missing entry means "nothing worth saying".
_REASONS = {
    "SEASON": {2: "Ideal for {season}", 1: "Can be grown in {season}"},
    "ZONE": {2: "Strong fit for your agro-climatic zone", 1: "Grows in your agro-climatic zone"},
    "WATER": {2: "Well suited to your watering method", 1: "Workable with your watering method"},
    "SOIL": {2: "Suits your soil type", 1: "Tolerates your soil type", 0: "Not ideal for your soil type"},
    "BUDGET": {2: "Fits your budget", 1: "Slightly above your budget", 0: "Above your budget"},
    "ROTATION": {
        2: "Good rotation after {previous}", 1: "Suits a field that was left empty",
        0: "Same crop family as {previous}",
    },
}


def match_reasons(
    factors: Optional[list], season: str, previous_crop: Optional[str] = None, limit: int = 4,
) -> list[str]:
    """Strengths first (ideal, then acceptable), then the one or two
    warnings a farmer should know about (budget, soil, rotation at 0)."""
    if not factors:
        return []
    context = {
        "season": _SEASON_NAMES.get(season, season.title()),
        "previous": previous_crop or "your previous crop",
    }
    points_by_factor = {f["factor"]: f["points"] for f in factors if isinstance(f, dict)}
    reasons: list[str] = []
    for wanted_points in (2, 1, 0):
        for factor, points in points_by_factor.items():
            template = _REASONS.get(factor, {}).get(points)
            if points == wanted_points and template:
                reasons.append(template.format(**context))
    return reasons[:limit]


__all__ = ["match_reasons"]
