"""Fresh-lead quota gating on pick_telecaller (DB-free).

Pins that `assignment_quota` caps the UNTOUCHED (New Lead) backlog on the create path:
an agent already at their fresh quota is dropped from the pool, the least-fresh
under-quota agent wins, and if everyone is at quota the lead stays unassigned (pick
returns None -> the 5-min sweep retries). `_at_quota` treats 0/None as uncapped. Run::

    python tests/test_quota_fresh_backlog.py     # or: pytest tests/test_quota_fresh_backlog.py
"""
import os
import sys
import asyncio
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
# assignmentService pulls in managers -> SharedBackend; add it like the runtime PYTHONPATH.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

import services.assignmentService as A  # noqa: E402


def _u(uid, quota):
    return SimpleNamespace(uid=uid, assignment_quota=quota, state="ka")


def test_at_quota_semantics():
    assert A._at_quota(_u("a", 30), 29) is False
    assert A._at_quota(_u("a", 30), 30) is True     # at quota -> full
    assert A._at_quota(_u("a", 30), 31) is True
    assert A._at_quota(_u("a", 0), 999) is False     # quota 0 = uncapped
    assert A._at_quota(_u("a", None), 5) is False    # quota None = uncapped


def _patch_pool(pool, fresh, today=None):
    """Swap the DB-touching helpers pick_telecaller calls for fakes. `fresh` gates the quota
    filter; `today` (leads given today) drives the fairness balance."""
    async def _fake_pool(engine, outlet_id, state, only_ids=None, *, allow_cross_state=True):
        return list(pool)

    async def _fake_fresh(engine, ids):
        return dict(fresh)

    async def _fake_today(engine, ids):
        return dict(today or {})

    A._active_telecallers = _fake_pool
    A._fresh_counts = _fake_fresh
    A._received_today_counts = _fake_today


def test_drops_agent_at_quota_then_balances_by_today():
    a, b, c = _u("a", 30), _u("b", 30), _u("c", 30)
    # 'a' is full on fresh backlog (excluded). Among under-quota b/c, balance by today-count.
    _patch_pool([a, b, c], fresh={"a": 30, "b": 10, "c": 5}, today={"a": 0, "b": 2, "c": 0})
    picked = asyncio.run(A.pick_telecaller("E", "outlet", "ka", enforce_quota=True))
    assert picked.uid == "c"     # fewest given today among under-quota; full 'a' excluded


def test_all_at_quota_returns_none():
    a, b = _u("a", 30), _u("b", 30)
    _patch_pool([a, b], {"a": 30, "b": 30})
    picked = asyncio.run(A.pick_telecaller("E", "outlet", "ka", enforce_quota=True))
    assert picked is None         # lead stays unassigned; the sweep retries later


def test_uncapped_agents_balanced_by_today():
    a, b = _u("a", 0), _u("b", 0)                       # both uncapped (quota 0)
    # Fresh backlog is huge but irrelevant to balance now; today-count decides.
    _patch_pool([a, b], fresh={"a": 500, "b": 3}, today={"a": 0, "b": 5})
    picked = asyncio.run(A.pick_telecaller("E", "outlet", "ka", enforce_quota=True))
    assert picked.uid == "a"     # fewest given today wins, regardless of fresh backlog


def test_ties_spread_across_equal_agents():
    """The whole point: equal agents must NOT all go to one (the uid-tie-break bug)."""
    a, b, c = _u("a", 0), _u("b", 0), _u("c", 0)
    picks = set()
    for _ in range(200):
        _patch_pool([a, b, c], fresh={}, today={"a": 0, "b": 0, "c": 2})  # c behind -> excluded from min
        picks.add(asyncio.run(A.pick_telecaller("E", "outlet", "ka", enforce_quota=True)).uid)
    assert picks == {"a", "b"}   # both least-today agents get picked; c (ahead) never does


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all fresh-backlog quota checks passed")
