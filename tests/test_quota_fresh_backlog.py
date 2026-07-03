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


def _patch_pool(pool, fresh):
    """Swap the two DB-touching helpers pick_telecaller calls for fakes."""
    async def _fake_pool(engine, outlet_id, state, only_ids=None, *, allow_cross_state=True):
        return list(pool)

    async def _fake_fresh(engine, ids):
        return dict(fresh)

    A._active_telecallers = _fake_pool
    A._fresh_counts = _fake_fresh


def test_drops_agent_at_quota_and_picks_least_fresh():
    a, b, c = _u("a", 30), _u("b", 30), _u("c", 30)
    _patch_pool([a, b, c], {"a": 30, "b": 10, "c": 5})   # 'a' is full
    picked = asyncio.run(A.pick_telecaller("E", "outlet", "ka", enforce_quota=True))
    assert picked.uid == "c"     # least fresh among under-quota; full 'a' excluded


def test_all_at_quota_returns_none():
    a, b = _u("a", 30), _u("b", 30)
    _patch_pool([a, b], {"a": 30, "b": 30})
    picked = asyncio.run(A.pick_telecaller("E", "outlet", "ka", enforce_quota=True))
    assert picked is None         # lead stays unassigned; the sweep retries later


def test_uncapped_agents_never_excluded():
    a, b = _u("a", 0), _u("b", 0)                       # both uncapped
    _patch_pool([a, b], {"a": 500, "b": 3})
    picked = asyncio.run(A.pick_telecaller("E", "outlet", "ka", enforce_quota=True))
    assert picked.uid == "b"     # nobody excluded; least fresh wins


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all fresh-backlog quota checks passed")
