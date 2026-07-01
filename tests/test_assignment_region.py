"""Region separation for lead auto-assignment (DB-free).

Pins the fix for the prod bug where Andhra Pradesh leads were handed to Karnataka
telecallers: a lead crosses state lines *only* when it has no state or its state is
genuinely unstaffed. If in-state agents merely happen to be offline / unavailable, the
lead waits (stays unassigned) instead of spilling to another state.

Two paths are covered because both do their own round-robin:
  - assignmentService._active_telecallers  (create + inbound routing)
  - leadService.distribute_leads (auto pool)   (admin bulk-distribute + scheduled sweep)

Run::

    python tests/test_assignment_region.py     # or: pytest tests/test_assignment_region.py
"""
import os
import sys
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

import services.assignmentService as A  # noqa: E402
import services.leadService as L        # noqa: E402


def run(coro):
    return asyncio.run(coro)


class _Res:
    def __init__(self, items):
        self.items = items
        self.count = len(items)


def _user(uid, state, outlet_id=None):
    return SimpleNamespace(uid=uid, state=state, outlet_id=outlet_id,
                           role=None, is_active=True)


def _patch_users(all_users):
    """Fake UserManager for assignmentService: honors the outlet_id filter, otherwise
    returns every (active telecaller) fake."""
    class _M:
        def __init__(self, engine):
            pass

        async def fetch_all(self, filters=None):
            f = filters or {}
            items = list(all_users)
            if "outlet_id" in f:
                items = [u for u in items if getattr(u, "outlet_id", None) == f["outlet_id"]]
            return _Res(items)

    A.UserManager = _M


# --- assignmentService._active_telecallers ---------------------------------------

def test_in_state_only_never_cross():
    # AP + KA agents present; an AP lead must draw ONLY the AP agent, never KA.
    _patch_users([_user("ap1", "andhra pradesh"), _user("ka1", "karnataka")])
    pool = run(A._active_telecallers("E", None, "andhra pradesh", only_ids={"ap1", "ka1"}))
    assert {u.uid for u in pool} == {"ap1"}, [u.uid for u in pool]


def test_in_state_offline_waits_not_cross():
    # THE BUG: AP agent exists but is offline (not in only_ids). Must return [] (lead
    # waits), NOT fall back to the Karnataka agent.
    _patch_users([_user("ap1", "andhra pradesh"), _user("ka1", "karnataka")])
    pool = run(A._active_telecallers("E", None, "andhra pradesh", only_ids={"ka1"}))
    assert pool == [], [u.uid for u in pool]


def test_unstaffed_state_crosses_as_last_resort():
    # Kerala has no telecaller at all -> documented last resort: cross state lines.
    _patch_users([_user("ka1", "karnataka")])
    pool = run(A._active_telecallers("E", None, "kerala", only_ids={"ka1"}))
    assert {u.uid for u in pool} == {"ka1"}, [u.uid for u in pool]


def test_no_state_uses_global_pool():
    _patch_users([_user("ka1", "karnataka"), _user("ap1", "andhra pradesh")])
    pool = run(A._active_telecallers("E", None, None, only_ids=None))
    assert {u.uid for u in pool} == {"ka1", "ap1"}, [u.uid for u in pool]


def test_outlet_tier_wins_over_state():
    # A telecaller tied to the resolved outlet is picked regardless of state (Tier 1).
    _patch_users([_user("o1", "karnataka", outlet_id="OUT"), _user("ap1", "andhra pradesh")])
    pool = run(A._active_telecallers("E", "OUT", "andhra pradesh", only_ids=None))
    assert {u.uid for u in pool} == {"o1"}, [u.uid for u in pool]


# --- leadService.distribute_leads (auto pool / sweep) ----------------------------

def _tc(uid, state, online=True):
    last = datetime.now(timezone.utc) if online else datetime(2000, 1, 1, tzinfo=timezone.utc)
    return SimpleNamespace(uid=uid, state=state, role=L.TELECALLER_ROLES[0],
                           is_active=True, last_active_at=last, assignment_quota=0)


def _patch_distribute(agents, leads, calls):
    class _UM:
        def __init__(self, engine):
            pass

        async def fetch_all(self, filters=None):
            return _Res(list(agents))

    class _LM:
        def __init__(self, engine):
            pass

        async def fetch_all(self, filters=None):
            return _Res([])                       # zero current load for everyone

        async def fetch(self, lid):
            return leads[lid]

    L.UserManager = _UM
    L.LeadManager = _LM

    async def _fake_reassign(engine, lead, tc_uid, by_user_id, reason=None):
        calls.append((lead.uid, tc_uid))
        return lead

    L.reassign = _fake_reassign


def test_distribute_auto_keeps_leads_in_state():
    # Pool: AP + KA online; a Telangana agent that is OFFLINE (so 'telangana' is staffed
    # but has no available agent).
    agents = [_tc("ap", "andhra pradesh"), _tc("ka", "karnataka"),
              _tc("tg", "telangana", online=False)]
    leads = {
        "L_ap": SimpleNamespace(uid="L_ap", state="andhra pradesh", deleted_at=None),
        "L_ka": SimpleNamespace(uid="L_ka", state="karnataka", deleted_at=None),
        "L_tg": SimpleNamespace(uid="L_tg", state="telangana", deleted_at=None),   # staffed+offline
        "L_kl": SimpleNamespace(uid="L_kl", state="kerala", deleted_at=None),      # unstaffed
        "L_no": SimpleNamespace(uid="L_no", state=None, deleted_at=None),          # no state
    }
    calls = []
    _patch_distribute(agents, leads, calls)
    res = run(L.distribute_leads("E", list(leads), None, by_user_id="system"))

    picked = dict(calls)
    assert picked.get("L_ap") == "ap"                 # in-state
    assert picked.get("L_ka") == "ka"                 # in-state
    assert "L_tg" not in picked                        # staffed-but-offline -> waits, not cross
    assert picked.get("L_kl") in ("ap", "ka")          # unstaffed -> cross-state last resort
    assert picked.get("L_no") in ("ap", "ka")          # no state -> global pool
    assert res["assigned"] == 4 and res["skipped"] == 1, res


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all assignment-region checks passed")
