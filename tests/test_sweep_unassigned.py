"""Pins the sweep_unassigned glue (DB-free).

The scheduled sweep is thin wiring on top of distribute_leads: pull every unassigned
lead, drop soft-deleted ones, and hand the rest to distribute_leads (which owns the
online + assignment_quota + least-loaded logic, tested elsewhere). These checks pin that
wiring: the IS-NULL filter, the deleted-lead exclusion, the (all-telecallers, "system")
call shape, and the empty-backlog no-op (don't call distribute with nothing). Run::

    python tests/test_sweep_unassigned.py     # or: pytest tests/test_sweep_unassigned.py
"""
import os
import sys
import asyncio
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
# leadService pulls in managers -> SharedBackend; add it like the runtime PYTHONPATH.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

import services.leadService as L  # noqa: E402


class _Res:
    def __init__(self, items):
        self.items = items
        self.count = len(items)


def _patch(unassigned_leads, distribute_calls):
    """Swap leadService's DB-touching globals for fakes; record distribute_leads calls."""
    L.get_settings = lambda: SimpleNamespace(name="test")
    L.get_engine = lambda *_a, **_k: "ENGINE"

    class _FakeLeadMgr:
        def __init__(self, engine):
            pass

        async def fetch_all(self, filters=None):
            assert filters == {"owner_id": None}   # pins the IS NULL backlog query
            return _Res(unassigned_leads)

    L.LeadManager = _FakeLeadMgr

    async def _fake_distribute(engine, lead_ids, telecaller_ids, by_user_id):
        distribute_calls.append((engine, lead_ids, telecaller_ids, by_user_id))
        return {"assigned": len(lead_ids), "skipped": 0, "by_telecaller": {}}

    L.distribute_leads = _fake_distribute


def test_distributes_only_live_unassigned():
    leads = [
        SimpleNamespace(uid="a", deleted_at=None),
        SimpleNamespace(uid="b", deleted_at=object()),   # soft-deleted -> must be dropped
        SimpleNamespace(uid="c", deleted_at=None),
    ]
    calls = []
    _patch(leads, calls)
    res = asyncio.run(L.sweep_unassigned())

    assert len(calls) == 1
    _engine, lead_ids, telecaller_ids, by_user_id = calls[0]
    assert lead_ids == ["a", "c"]                # deleted 'b' excluded
    assert telecaller_ids is None               # whole online pool
    assert by_user_id == "system"               # system actor, not a human admin
    assert res["assigned"] == 2


def test_noop_when_backlog_empty():
    calls = []
    _patch([], calls)
    res = asyncio.run(L.sweep_unassigned())

    assert calls == []                          # distribute_leads never called
    assert res == {"assigned": 0, "skipped": 0}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all sweep_unassigned checks passed")
