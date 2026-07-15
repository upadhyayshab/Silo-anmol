"""Pins the lead-mutation scope split on the leads router (DB-free).

The deliberate call (commit 26f11fb, "feat(crm): telecallers can service any
searched lead (owner unchanged)"): row-scope was intentionally DROPPED from the
lead WRITE endpoints — disposition (POST /leads/{id}/calls), notes, stage, and
field edits. A telecaller can now work a lead they found via search; ownership is
unchanged and every action is attributed via activity.user_id. Only OWNERSHIP-
CHANGING routes still gate on ownership: assign_lead (POST /leads/{id}/assign)
keeps calling _assert_lead_in_scope.

Why this test exists: these two facts pull in opposite directions and a future
"simplify"/"harden" pass could quietly re-add `await _assert_lead_in_scope(...)`
to log_lead_call and silently regress the open-dispositioning decision, or drop
the guard from assign_lead. So we pin BOTH halves of the split:

  * log_lead_call dispositions an UNOWNED lead with NO inbound call-access -> NO
    403 (open). If someone re-adds the scope guard, has_call_access is stubbed
    False here, so the endpoint would raise 403 and THIS test fails -> forcing a
    conscious decision rather than a silent behavior change.
  * assign_lead reassigning an UNOWNED lead (no call-access) -> still 403.

Everything DB-touching is faked (managers, leadService.log_call, has_call_access);
no live DB, no network. Run::

    python tests/test_leads_scope.py     # or: pytest tests/test_leads_scope.py
"""
import os
import sys
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:  # path_setup prints a banner; keep it from blowing up a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports


def _lead(uid="leads_1", owner_id="owner", deleted_at=None):
    return SimpleNamespace(uid=uid, owner_id=owner_id, deleted_at=deleted_at)


def _actor(uid):
    # _crm_actor reads .last_active_at (recent -> presence update skipped) + .full_name.
    return SimpleNamespace(uid=uid, full_name="Agent Smith",
                           last_active_at=datetime.now(timezone.utc))


def _activity(lead_uid):
    # log_lead_call returns LeadActivityResponse.model_validate(activity) (from_attributes).
    return SimpleNamespace(uid="act_1", lead_id=lead_uid, user_id="caller",
                           activity_type="CALL_LOG", body="Connected", outcome="answered",
                           from_stage=None, to_stage=None, details=None,
                           created_at=datetime.now(timezone.utc))


class _Patched:
    """Fake out every DB/collaborator touchpoint the two handlers use, so the ONLY
    thing under test is the presence/absence of the row-scope gate. Restores on exit."""

    def __init__(self, L, lead, *, log_call_spy=None):
        self.L = L
        self.lead = lead
        self.log_call_spy = log_call_spy
        self._saved = []

    def _set(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def __enter__(self):
        L, lead = self.L, self.lead

        async def _fetch_lead(lead_id):
            return lead

        async def _fetch_user(user_id):
            # A "missing_*" id models an unknown target user (assign_lead wraps this
            # lookup in try/except -> 404); everyone else resolves (used by _crm_actor).
            if str(user_id).startswith("missing"):
                raise LookupError("no such user")
            return _actor(user_id)

        async def _update_user(*a, **k):
            return None

        async def _has_call_access(engine, lead_id, telecaller_id):
            return False  # no inbound grant -> a re-added scope guard would 403

        async def _log_call(engine, lead_arg, *a, **k):
            if self.log_call_spy is not None:
                self.log_call_spy["called"] = True
            return _activity(lead_arg.uid)

        self._set(L.lead_manager, "fetch", _fetch_lead)
        self._set(L.user_manager, "fetch", _fetch_user)
        self._set(L.user_manager, "update", _update_user)
        self._set(L.assignmentService, "has_call_access", _has_call_access)
        self._set(L.leadService, "log_call", _log_call)
        return self

    def __exit__(self, *exc):
        for obj, name, val in reversed(self._saved):
            setattr(obj, name, val)
        return False


def test_disposition_on_unowned_lead_is_open_no_403():
    """A LEADS_WRITE telecaller who does NOT own the lead and has NO call-access can
    still POST /leads/{id}/calls (disposition) — intended per 26f11fb."""
    import routers.v1.leads as L
    from utils.auth import AuthContext
    from models import CallLogRequest
    from fastapi import BackgroundTasks

    lead = _lead(owner_id="someone_else")
    ctx = AuthContext(user_id="caller", role="TELECALLER", scope_level="OUTLET")
    spy = {"called": False}

    with _Patched(L, lead, log_call_spy=spy):
        resp = asyncio.run(L.log_lead_call(
            lead.uid,
            CallLogRequest(disposition="Connected", sub_disposition="Interested", note="hi"),
            BackgroundTasks(),
            ctx=ctx,
            provider=None,
        ))

    # No HTTPException raised, the disposition was actually recorded, and it came back
    # as a normal activity response (not a 403).
    assert spy["called"] is True, "leadService.log_call should have run (write went through)"
    assert resp.lead_id == lead.uid
    assert resp.activity_type == "CALL_LOG"


def test_reassign_unowned_lead_still_403():
    """The one guard that REMAINS: assign_lead (ownership change) rejects a non-owner
    telecaller with no call-access — documents the intended split (writes open,
    reassignment gated)."""
    import routers.v1.leads as L
    from utils.auth import AuthContext
    from models import AssignRequest
    from fastapi import HTTPException

    lead = _lead(owner_id="someone_else")
    ctx = AuthContext(user_id="caller", role="TELECALLER", scope_level="OUTLET")

    with _Patched(L, lead):
        try:
            asyncio.run(L.assign_lead(
                lead.uid, AssignRequest(telecaller_id="target"), ctx=ctx))
            assert False, "expected a 403 reassigning a lead the caller does not own"
        except HTTPException as e:
            assert e.status_code == 403


def test_reassign_owned_lead_passes_scope_gate():
    """Sanity twin: the OWNER clears assign_lead's scope gate (proving the 403 above
    is about ownership, not a blanket denial). Target lookup is stubbed to fail with a
    404 AFTER the scope check, so reaching that 404 == the scope gate let the owner
    through."""
    import routers.v1.leads as L
    from utils.auth import AuthContext
    from models import AssignRequest
    from fastapi import HTTPException

    lead = _lead(owner_id="caller")  # caller OWNS this lead
    ctx = AuthContext(user_id="caller", role="TELECALLER", scope_level="OUTLET")

    with _Patched(L, lead):
        try:
            asyncio.run(L.assign_lead(
                lead.uid, AssignRequest(telecaller_id="missing_target"), ctx=ctx))
            assert False, "expected to progress past the scope gate to the target lookup"
        except HTTPException as e:
            # 404 = we cleared _assert_lead_in_scope and failed later at target fetch;
            # a 403 here would mean the owner was wrongly fenced out.
            assert e.status_code == 404, f"owner should pass the scope gate, got {e.status_code}"


if __name__ == "__main__":
    test_disposition_on_unowned_lead_is_open_no_403()
    test_reassign_unowned_lead_still_403()
    test_reassign_owned_lead_passes_scope_gate()
    print("OK")
