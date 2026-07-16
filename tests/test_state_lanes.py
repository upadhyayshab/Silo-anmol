"""State-Lane dashboard: pure rollup (assemble_lanes) + distribute wiring. DB-free.

Run::
    python tests/test_state_lanes.py     # or: pytest tests/test_state_lanes.py
"""
import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

import services.stateLaneService as S  # noqa: E402

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _base_kwargs():
    return dict(
        pages=[
            {"page_id": "p_mh", "page_name": "Silo MH", "routing_state": "Maharashtra"},
            {"page_id": "p_ka", "page_name": "Silo KA", "routing_state": "Karnataka"},
            {"page_id": "p_x", "page_name": "Unset Page", "routing_state": None},
        ],
        leads_in_by_state={"Maharashtra": 50, "Karnataka": 10, None: 3},
        leads_in_by_page={"p_mh": 42, "p_ka": 9, "p_x": 3},
        unassigned_by_state={"Maharashtra": 7, None: 3},
        load_by_owner={"u_asha": 30, "u_ravi": 5},
        telecallers=[
            {"uid": "u_asha", "name": "Asha", "state": "Maharashtra", "quota": 40,
             "last_active_at": NOW - timedelta(minutes=5)},    # online
            {"uid": "u_ravi", "name": "Ravi", "state": "Karnataka", "quota": 20,
             "last_active_at": NOW - timedelta(minutes=90)},   # offline
        ],
        now=NOW,
    )


def test_maharashtra_lane_rollup():
    out = S.assemble_lanes(**_base_kwargs())
    mh = next(l for l in out["lanes"] if l["state"] == "Maharashtra")
    assert mh["is_mapped"] is True
    assert mh["leads_in"] == 50
    assert mh["unassigned"] == 7
    assert mh["capacity"] == 40
    assert mh["load"] == 30
    assert [p["page_id"] for p in mh["pages"]] == ["p_mh"]
    assert mh["pages"][0]["leads_in"] == 42
    asha = mh["telecallers"][0]
    assert asha["uid"] == "u_asha" and asha["online"] is True and asha["fill_pct"] == 75


def test_offline_and_fill_pct_zero_quota():
    kw = _base_kwargs()
    kw["telecallers"][1]["quota"] = 0            # Ravi: no quota
    out = S.assemble_lanes(**kw)
    ka = next(l for l in out["lanes"] if l["state"] == "Karnataka")
    ravi = ka["telecallers"][0]
    assert ravi["online"] is False and ravi["fill_pct"] == 0 and ka["capacity"] == 0


def test_fill_pct_capped_at_100():
    # A manual super-admin pick bypasses the quota, so assigned-today can exceed it: Asha
    # gets 50 against a quota of 40 -> bar caps at 100%, not 125%.
    kw = _base_kwargs()
    kw["load_by_owner"]["u_asha"] = 50
    out = S.assemble_lanes(**kw)
    mh = next(l for l in out["lanes"] if l["state"] == "Maharashtra")
    assert mh["telecallers"][0]["fill_pct"] == 100     # min(100, 125)


def test_unrouted_and_other_lanes():
    out = S.assemble_lanes(**_base_kwargs())
    unrouted = next(l for l in out["lanes"] if l["state"] == S.UNROUTED)
    assert unrouted["is_mapped"] is False
    assert [p["page_id"] for p in unrouted["pages"]] == ["p_x"]
    assert unrouted["leads_in"] == 3            # sum of its pages' leads_in
    other = next(l for l in out["lanes"] if l["state"] == S.OTHER)
    assert other["is_mapped"] is False and other["unassigned"] == 3 and other["leads_in"] == 3


def test_totals():
    out = S.assemble_lanes(**_base_kwargs())["totals"]
    assert out["leads_in"] == 63              # 50 + 10 + 3
    assert out["unassigned"] == 10            # 7 + 3
    assert out["capacity"] == 60              # 40 + 20
    assert out["load"] == 35                  # 30 + 5


def _patch_distribute(unassigned_leads, calls):
    """Fake LeadManager.fetch_all + leadService.distribute_leads (mirrors test_sweep_unassigned)."""
    class _Res:
        def __init__(self, items): self.items = items

    class _FakeLeadMgr:
        def __init__(self, engine): pass
        async def fetch_all(self, filters=None):
            assert filters == {"owner_id": None, "state": "Maharashtra"}   # pins the backlog query
            return _Res(unassigned_leads)

    S.LeadManager = _FakeLeadMgr

    async def _fake_distribute(engine, lead_ids, telecaller_ids, by_user_id):
        calls.append((lead_ids, telecaller_ids, by_user_id))
        return {"assigned": len(lead_ids), "skipped": 0, "by_telecaller": {}}
    S.leadService = SimpleNamespace(distribute_leads=_fake_distribute)


def test_distribute_state_only_live_backlog():
    leads = [SimpleNamespace(uid="a", deleted_at=None),
             SimpleNamespace(uid="b", deleted_at=object()),   # soft-deleted -> dropped
             SimpleNamespace(uid="c", deleted_at=None)]
    calls = []
    _patch_distribute(leads, calls)
    res = asyncio.run(S.distribute_state_backlog("ENGINE", "Maharashtra", by_user_id="admin1"))
    assert len(calls) == 1
    lead_ids, telecaller_ids, by_user_id = calls[0]
    assert lead_ids == ["a", "c"] and telecaller_ids is None and by_user_id == "admin1"
    assert res["assigned"] == 2


def test_distribute_state_noop_when_empty():
    calls = []
    _patch_distribute([], calls)
    res = asyncio.run(S.distribute_state_backlog("ENGINE", "Maharashtra", by_user_id="admin1"))
    assert calls == [] and res["assigned"] == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all state-lane checks passed")
