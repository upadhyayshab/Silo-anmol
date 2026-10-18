"""Plain-language reasons for a recommendation, built from the factor points
the scoring engine already saved with each result (0 = poor, 1 = acceptable,
2 = ideal). Shared by the wizard submit response and the report dossiers so
both screens explain a crop the same way. Text lives in utils.farmerText."""
from typing import Optional

from utils.farmerText import previous_crop_fallback, reason_template, season_name


def match_reasons(
    factors: Optional[list], season: str, previous_crop: Optional[str] = None, limit: int = 4,
    lang: str = "en",
) -> list[str]:
    """Strengths first (ideal, then acceptable), then the one or two
    warnings a farmer should know about (budget, soil, rotation at 0)."""
    if not factors:
        return []
    context = {
        "season": season_name(season, lang),
        "previous": previous_crop or previous_crop_fallback(lang),
    }
    points_by_factor = {f["factor"]: f["points"] for f in factors if isinstance(f, dict)}
    reasons: list[str] = []
    for wanted_points in (2, 1, 0):
        for factor, points in points_by_factor.items():
            template = reason_template(factor, points, lang)
            if points == wanted_points and template:
                reasons.append(template.format(**context))
    return reasons[:limit]


__all__ = ["match_reasons"]
