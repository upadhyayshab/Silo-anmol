"""Inbound routing picks its region from the ExoPhone the customer dialed (DB-free).

The wiring, not the policy (policy lives in test_assignment_region / test_exophone_config):

  - a KNOWN lead still routes by the lead's own state, and never crosses state lines;
  - an UNKNOWN caller is scoped to the state(s) the dialed number serves, but may still
    cross if that region is entirely unstaffed (unchanged from today);
  - an unmapped dialed number yields no states -> today's global available pool.

Run::

    python tests/test_inbound_routing.py     # or: pytest tests/test_inbound_routing.py
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

import services.telephonyService as T      # noqa: E402
import services.assignmentService as A     # noqa: E402
import services.presenceService as P       # noqa: E402
import services.exophoneService as X       # noqa: E402

TG_AP = "+914012345678"
STATES = {"Telangana": TG_AP, "Andhra Pradesh": TG_AP}


def run(coro):
    return asyncio.run(coro)


def _patch(lead, dialed_states):
    """Returns a dict that captures the kwargs pick_telecaller was called with."""
    seen = {}

    async def _find_duplicate(engine, **kw):
        return lead

    async def _available_ids(engine):
        return {"someone"}

    async def _states_for_dialed(engine, dialed):
        return list(dialed_states)

    async def _pick(engine, outlet_id, state=None, only_ids=None, **kw):
        seen["state"] = state
        seen["allow_cross_state"] = kw.get("allow_cross_state")
        return None                      # -> no_agent; keeps UserManager out of the test

    T.dedup_utils.find_duplicate = _find_duplicate
    P.available_ids = _available_ids
    X.states_for_dialed = _states_for_dialed
    A.pick_telecaller = _pick
    return seen


def test_unknown_caller_scoped_to_the_dialed_numbers_states():
    seen = _patch(lead=None, dialed_states=["Telangana", "Andhra Pradesh"])
    route = run(T.resolve_inbound_agent("E", "+919812345678", dialed_number=TG_AP))
    assert seen["state"] == ["Telangana", "Andhra Pradesh"], seen
    # unknown caller: no lead to protect, so an unstaffed region may still cross (as today)
    assert seen["allow_cross_state"] is True, seen
    assert route.reason == "no_agent"


def test_unmapped_dialed_number_falls_back_to_global_pool():
    seen = _patch(lead=None, dialed_states=[])
    run(T.resolve_inbound_agent("E", "+919812345678", dialed_number="+918068875144"))
    assert seen["state"] == [], seen
    assert seen["allow_cross_state"] is True, seen


def test_no_dialed_number_is_todays_behaviour():
    seen = _patch(lead=None, dialed_states=["Telangana"])
    run(T.resolve_inbound_agent("E", "+919812345678"))     # no dialed_number
    assert seen["state"] is None, seen
    assert seen["allow_cross_state"] is True, seen


def test_known_lead_uses_its_own_state_and_ignores_the_dialed_number():
    lead = SimpleNamespace(uid="L1", owner_id=None, outlet_id=None, state="Karnataka")
    seen = _patch(lead=lead, dialed_states=["Telangana", "Andhra Pradesh"])
    run(T.resolve_inbound_agent("E", "+919812345678", dialed_number=TG_AP))
    assert seen["state"] == "Karnataka", seen
    # THE REGRESSION GUARD: a known lead must never cross state lines.
    assert seen["allow_cross_state"] is False, seen


def test_known_lead_without_state_still_scoped_by_dialed_number():
    lead = SimpleNamespace(uid="L1", owner_id=None, outlet_id=None, state=None)
    seen = _patch(lead=lead, dialed_states=["Telangana"])
    run(T.resolve_inbound_agent("E", "+919812345678", dialed_number=TG_AP))
    assert seen["state"] == ["Telangana"], seen
    assert seen["allow_cross_state"] is True, seen


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all inbound-routing checks passed")
