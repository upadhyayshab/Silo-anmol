"""State <-> ExoPhone map: outbound caller-ID and inbound pool scoping.

One flat `{state: exophone}` map plus a default. A *region* is an emergent property of
the map, not an entity: Telangana and Andhra Pradesh are one region because both point
at the same number. Reverse the map to learn which states a dialed number serves.

Stored in the existing `app_settings` key/JSON table — adding a key needs no migration.

ponytail: no cache. `get_map()` is one indexed SELECT over a couple of rows, and both
callers (inbound call, softphone open) are low-frequency. Add a cache only if it shows
up in a profile.
"""
from typing import Dict, List, Optional, Tuple

from managers import AppSettingManager
from utils.constants import (
    DEFAULT_EXOPHONE, SETTING_DEFAULT_EXOPHONE, SETTING_IVR_DIGITS,
    SETTING_STATE_EXOPHONES,
)
from utils.dedup_utils import normalize_mobile

_KEYS = [SETTING_DEFAULT_EXOPHONE, SETTING_STATE_EXOPHONES]


def _key(state: Optional[str]) -> str:
    return (state or "").strip().lower()


# --------------------------------------------------------------------------
# Pure helpers (no DB) — the whole policy lives here
# --------------------------------------------------------------------------

def resolve_desired_vn(state: Optional[str], default_vn: str,
                       overrides: Optional[Dict[str, str]]) -> str:
    """The ExoPhone an agent in `state` should place outbound calls from."""
    target = _key(state)
    if target:
        for s, exo in (overrides or {}).items():
            if exo and _key(s) == target:
                return exo
    return default_vn or DEFAULT_EXOPHONE


def states_for_exophone(dialed: Optional[str],
                        overrides: Optional[Dict[str, str]]) -> List[str]:
    """Reverse the map: which states does the dialed ExoPhone serve? Two states sharing
    one number both come back — that IS the region. Unknown number -> [] -> the caller
    falls through to today's global pool."""
    d = normalize_mobile(dialed)
    if not d:
        return []
    return [s for s, exo in (overrides or {}).items() if normalize_mobile(exo) == d]


def exophone_for_digit(digit: Optional[str],
                       ivr_digits: Optional[Dict[str, str]]) -> Optional[str]:
    """The IVR digit is an ALIAS for an ExoPhone, not a second region definition.

    Today there is one published number, so the caller's language choice is the only region
    signal. Each Gather branch hits `/exotel/inbound?digit=N`; we translate N to the region's
    number and hand it to the SAME reverse lookup the dialed number uses. Region membership
    therefore lives only in `state_exophones`.

    Returns None for an unknown/blank digit -> caller falls through to the dialed number.
    """
    d = str(digit or "").strip()
    if not d:
        return None
    return (ivr_digits or {}).get(d) or None


def build_drift_plan(agents, mappings, default_vn: str,
                     overrides: Optional[Dict[str, str]]) -> List[dict]:
    """One row per CRM telecaller: what their caller-ID IS vs what it SHOULD be.

    `drift` is True only when the agent is mapped AND the numbers differ once normalized
    — an unmapped agent has nothing to PUT (they auto-map on first softphone open), and
    '08068875144' vs '+918068875144' is the same number.
    """
    by_email = {str(m.get("AppUserId") or "").strip().lower(): m for m in mappings}
    out: List[dict] = []
    for a in agents:
        email = (getattr(a, "email", "") or "").strip().lower()
        state = getattr(a, "state", None)
        mapped_vn = (by_email.get(email) or {}).get("VirtualNumber")
        desired_vn = resolve_desired_vn(state, default_vn, overrides)
        drift = bool(mapped_vn) and normalize_mobile(mapped_vn) != normalize_mobile(desired_vn)
        out.append({
            "uid": getattr(a, "uid", None),
            "name": getattr(a, "full_name", "") or email,
            "email": email,
            "state": state,
            "mapped_vn": mapped_vn,
            "desired_vn": desired_vn,
            "drift": drift,
        })
    return out


# --------------------------------------------------------------------------
# DB-backed accessors
# --------------------------------------------------------------------------

async def load(engine) -> Tuple[str, Dict[str, str]]:
    """(default_exophone, {state: exophone}). Missing rows -> code defaults."""
    kv = await AppSettingManager(engine).get_map(_KEYS)
    default_vn = kv.get(SETTING_DEFAULT_EXOPHONE) or DEFAULT_EXOPHONE
    overrides = kv.get(SETTING_STATE_EXOPHONES)
    if not isinstance(overrides, dict):
        overrides = {}
    return default_vn, overrides


async def desired_vn_for(engine, state: Optional[str]) -> str:
    default_vn, overrides = await load(engine)
    return resolve_desired_vn(state, default_vn, overrides)


async def states_for_dialed(engine, dialed: Optional[str]) -> List[str]:
    _, overrides = await load(engine)
    return states_for_exophone(dialed, overrides)


async def load_ivr_digits(engine) -> Dict[str, str]:
    """{digit: exophone} for the temporary IVR bridge. Missing/!dict -> {} (no IVR)."""
    kv = await AppSettingManager(engine).get_map([SETTING_IVR_DIGITS])
    digits = kv.get(SETTING_IVR_DIGITS)
    return digits if isinstance(digits, dict) else {}


async def exophone_for_digit_db(engine, digit: Optional[str]) -> Optional[str]:
    """Digit -> the region's ExoPhone, or None. Skips the DB read when no digit was sent,
    so the post-IVR world (no `digit` param) costs nothing."""
    if not str(digit or "").strip():
        return None
    return exophone_for_digit(digit, await load_ivr_digits(engine))

# ponytail: no `__main__` smoke block — this module's app-relative imports mean it can't
# run standalone (same as telephonyService). tests/test_exophone_config.py covers every
# pure branch; a check that can't be executed is worse than no check.
