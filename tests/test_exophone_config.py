"""State <-> ExoPhone map: outbound caller-ID + inbound pool scoping (DB-free).

Pins two things:
  - a state with no override falls back to the default ExoPhone (so shipping with an
    empty map changes nothing);
  - two states sharing one number reverse-lookup to BOTH (AP + Telangana are one
    region precisely because they share a number).

Run::

    python tests/test_exophone_config.py     # or: pytest tests/test_exophone_config.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

import services.exophoneService as X  # noqa: E402

DEFAULT = "+918068875144"
TG_AP = "+914012345678"
OVERRIDES = {
    "Telangana": TG_AP,
    "Andhra Pradesh": TG_AP,
    "Punjab": "+911723456789",
}


# --- outbound: which number does an agent call out from? --------------------------

def test_override_wins():
    assert X.resolve_desired_vn("Punjab", DEFAULT, OVERRIDES) == "+911723456789"


def test_two_states_share_one_number():
    assert X.resolve_desired_vn("Telangana", DEFAULT, OVERRIDES) == TG_AP
    assert X.resolve_desired_vn("Andhra Pradesh", DEFAULT, OVERRIDES) == TG_AP


def test_state_without_override_falls_back():
    assert X.resolve_desired_vn("Karnataka", DEFAULT, OVERRIDES) == DEFAULT


def test_no_state_falls_back():
    assert X.resolve_desired_vn(None, DEFAULT, OVERRIDES) == DEFAULT


def test_empty_map_falls_back():
    # THE ROLLOUT GUARANTEE: no config rows -> every agent gets the default.
    assert X.resolve_desired_vn("Punjab", DEFAULT, {}) == DEFAULT


def test_state_match_is_case_and_space_insensitive():
    assert X.resolve_desired_vn("  telangana ", DEFAULT, OVERRIDES) == TG_AP


# --- inbound: which states does the dialed number serve? --------------------------

def test_reverse_lookup_returns_both_states_of_a_shared_number():
    got = set(X.states_for_exophone(TG_AP, OVERRIDES))
    assert got == {"Telangana", "Andhra Pradesh"}, got


def test_reverse_lookup_normalizes_format():
    # Exotel stores the same number as '+9140...' and '040...'.
    assert set(X.states_for_exophone("04012345678", OVERRIDES)) == {"Telangana", "Andhra Pradesh"}


def test_reverse_lookup_unknown_number_is_empty():
    # -> inbound falls back to today's global pool. No regression.
    assert X.states_for_exophone(DEFAULT, OVERRIDES) == []
    assert X.states_for_exophone("", OVERRIDES) == []


# --- the admin drift plan ----------------------------------------------------------

def _agent(uid, email, state):
    return SimpleNamespace(uid=uid, email=email, full_name=uid, state=state)


def test_drift_plan_flags_only_real_changes():
    agents = [
        _agent("a", "a@x.com", "Punjab"),        # mapped to default -> must move
        _agent("b", "b@x.com", "Karnataka"),     # already on default
        _agent("c", "c@x.com", "Punjab"),        # same number, other format -> NO drift
        _agent("d", "d@x.com", "Punjab"),        # unmapped -> no PUT possible
    ]
    mappings = [
        {"AppUserId": "a@x.com", "VirtualNumber": DEFAULT},
        {"AppUserId": "b@x.com", "VirtualNumber": DEFAULT},
        {"AppUserId": "c@x.com", "VirtualNumber": "01723456789"},
    ]
    plan = X.build_drift_plan(agents, mappings, DEFAULT, OVERRIDES)
    by = {p["email"]: p for p in plan}

    assert by["a@x.com"]["drift"] is True
    assert by["a@x.com"]["desired_vn"] == "+911723456789"
    assert by["b@x.com"]["drift"] is False
    assert by["c@x.com"]["drift"] is False, "same number, different format"
    assert by["d@x.com"]["mapped_vn"] is None and by["d@x.com"]["drift"] is False


def test_drift_plan_matches_email_case_insensitively():
    agents = [_agent("a", "A@X.com", "Punjab")]
    mappings = [{"AppUserId": "a@x.com", "VirtualNumber": DEFAULT}]
    plan = X.build_drift_plan(agents, mappings, DEFAULT, OVERRIDES)
    assert plan[0]["mapped_vn"] == DEFAULT and plan[0]["drift"] is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all exophone-config checks passed")
