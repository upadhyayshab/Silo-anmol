"""Pins release_leads_of_users: the deactivation cleanup that frees a dead owner's leads.

When telecallers are deactivated their leads must be un-owned (owner_id -> NULL) so the
5-min sweep redistributes them; otherwise they sit stranded (sweep only takes IS NULL).
These checks are DB-free: empty input is a no-op (never touches the DB), and a non-empty
call issues exactly one UPDATE and returns its rowcount. Run::

    python tests/test_release_leads_of_users.py   # or: pytest tests/test_release_leads_of_users.py
"""
import os
import sys
import asyncio
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

import services.leadService as L  # noqa: E402


class _FakeSession:
    def __init__(self, rowcount, executed):
        self._rowcount, self._executed = rowcount, executed
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        self._executed.append(stmt)
        return SimpleNamespace(rowcount=self._rowcount)

    async def commit(self):
        self.committed = True


def _patch(rowcount, executed):
    class _FakeLeadMgr:
        def __init__(self, engine):
            pass

        def session_factory(self):
            return _FakeSession(rowcount, executed)

    L.LeadManager = _FakeLeadMgr


def test_empty_is_noop():
    executed = []
    _patch(0, executed)
    assert asyncio.run(L.release_leads_of_users("ENGINE", [])) == 0
    assert executed == []                     # no DB call for an empty roster


def test_releases_and_returns_count():
    executed = []
    _patch(7, executed)
    n = asyncio.run(L.release_leads_of_users("ENGINE", ["u1", "u2"]))
    assert n == 7                             # rowcount passthrough
    assert len(executed) == 1                 # exactly one UPDATE issued
    assert executed[0].is_update              # it's an UPDATE, not a SELECT


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all release_leads_of_users checks passed")
