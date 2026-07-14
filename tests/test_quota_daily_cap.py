"""Daily-quota gating on pick_telecaller (DB-free).

Pins the new rule: `assignment_quota` is a HARD daily cap on leads RECEIVED today
(created_at >= 00:00 IST), not on the still-untouched backlog. Working a lead no longer
frees a slot, so auto-assignment stops at exactly the quota and the overflow stays
unassigned for a super admin to place. Same today-count both gates the pool and balances
it (fewest-given-today wins). `_at_quota` treats 0/None as uncapped. Run::

    python tests/test_quota_daily_cap.py     # or: pytest tests/test_quota_daily_cap.py
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
    assert A._at_quota(_u("a", 10), 9) is False
    assert A._at_quota(_u("a", 10), 10) is True     # hit the daily cap
    assert A._at_quota(_u("a", 10), 11) is True
    assert A._at_quota(_u("a", 0), 999) is False     # quota 0 = uncapped
    assert A._at_quota(_u("a", None), 5) is False    # quota None = uncapped


def _patch_pool(pool, today):
    """Swap the DB-touching helpers pick_telecaller calls for fakes. `today` (leads received
    today) both gates the quota filter and drives the fairness balance."""
    async def _fake_pool(engine, outlet_id, state, only_ids=None, *, allow_cross_state=True):
        return list(pool)

    async def _fake_today(engine, ids):
        return dict(today)

    A._active_telecallers = _fake_pool
    A._received_today_counts = _fake_today


def test_stops_at_quota_and_balances_by_today():
    a, b, c = _u("a", 10), _u("b", 10), _u("c", 10)
    # 'a' already got 10 today -> excluded even though a fast worker may have 0 left un-worked.
    _patch_pool([a, b, c], today={"a": 10, "b": 6, "c": 4})
    picked = asyncio.run(A.pick_telecaller("E", "outlet", "ka", enforce_quota=True))
    assert picked.uid == "c"     # fewest given today among under-cap; full 'a' excluded


def test_all_at_quota_returns_none():
    a, b = _u("a", 10), _u("b", 10)
    _patch_pool([a, b], {"a": 10, "b": 10})
    picked = asyncio.run(A.pick_telecaller("E", "outlet", "ka", enforce_quota=True))
    assert picked is None         # both hit the daily cap -> unassigned; a super admin places it


def test_uncapped_agents_balanced_by_today():
    a, b = _u("a", 0), _u("b", 0)                      # both uncapped (quota 0)
    _patch_pool([a, b], today={"a": 2, "b": 5})
    picked = asyncio.run(A.pick_telecaller("E", "outlet", "ka", enforce_quota=True))
    assert picked.uid == "a"     # fewest given today wins


def test_ties_spread_across_equal_agents():
    """Equal agents must NOT all go to one (the uid-tie-break bug)."""
    a, b, c = _u("a", 10), _u("b", 10), _u("c", 10)
    picks = set()
    for _ in range(200):
        _patch_pool([a, b, c], today={"a": 0, "b": 0, "c": 2})  # c ahead -> excluded from min
        picks.add(asyncio.run(A.pick_telecaller("E", "outlet", "ka", enforce_quota=True)).uid)
    assert picks == {"a", "b"}   # both least-today agents get picked; c (ahead) never does


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all daily-cap quota checks passed")
