"""Cluster geography helpers.

The cluster CSV uses display district names with parentheses
(e.g. "Ballari (Bellary)", "Mysuru (Mysore)"). These must be normalised to the
**exact lowercase canonical district strings already stored in `outlet_mappings`**,
otherwise the district -> cluster analytics join silently drops rows.

IMPORTANT: we keep a *small* cluster-specific alias map rather than reusing the large
``utils.outlet_assignment.DISTRICT_ALIASES``, and otherwise validate against the live
``outlet_mappings`` district set.

One deliberate alignment: ``"ballari" -> "vijayanagara"``. Geographically these are two
districts (Vijayanagara was carved out of Ballari in 2021), and the cluster CSV lists both
under Central Karnataka. But the existing ERP data convention (outlet_mappings + orders,
seeded via outlet_assignment) already stores all Ballari taluks under ``vijayanagara``.
For cluster rollups to join correctly we MUST follow that same convention, so we fold
Ballari into Vijayanagara here too (both land in the same cluster, so no analytics is lost).
Revisit this if outlet_mappings is ever split to track Ballari separately.
"""
import re
from typing import Iterable, Optional

# Cluster-CSV district spelling -> canonical lowercase name stored in outlet_mappings.
# Keep this minimal: only the deltas where the CSV spelling differs from what the
# outlet_mappings seed stored. Validate the rest against the live DB.
CLUSTER_DISTRICT_ALIASES = {
    "davangere": "davanagere",
    "koppala": "koppal",
    "chamarajanagar": "chamarajanagara",
    "chamrajnagar": "chamarajanagara",
    "chikkaballapur": "chikkaballapura",
    "bangalore rural": "bengaluru rural",
    "bengaluru urban": "bengaluru",
    "bangalore urban": "bengaluru",
    # ERP stores Ballari taluks under Vijayanagara (see module docstring). Same cluster.
    "ballari": "vijayanagara",
    "bellary": "vijayanagara",
}

_PAREN_RE = re.compile(r"\([^)]*\)")


def strip_parenthetical(name: str) -> str:
    """"Ballari (Bellary)" -> "Ballari"; collapses repeated whitespace."""
    if name is None:
        return ""
    cleaned = _PAREN_RE.sub("", str(name))
    return re.sub(r"\s+", " ", cleaned).strip()


def canonical_district(name: str) -> str:
    """Normalise a CSV district name to the lowercase canonical form.

    Strips the parenthetical alias, lowercases, then applies the cluster-specific
    alias map. The result is *intended* to match an outlet_mappings district; callers
    should still validate against the live set (see ``match_district``).
    """
    base = strip_parenthetical(name).lower()
    return CLUSTER_DISTRICT_ALIASES.get(base, base)


def match_district(name: str, known_districts: Iterable[str]) -> Optional[str]:
    """Return the canonical district if it exists in ``known_districts``, else None.

    ``known_districts`` should be the distinct lowercase ``district`` values from
    ``outlet_mappings`` (the authoritative set). This is what makes the seed
    self-validating instead of trusting a hardcoded alias map forever.
    """
    known = {d.lower() for d in known_districts if d}
    candidate = canonical_district(name)
    if candidate in known:
        return candidate
    # Fall back to the raw stripped/lowered form in case the alias map over-mapped.
    raw = strip_parenthetical(name).lower()
    if raw in known:
        return raw
    return None
