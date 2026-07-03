"""Pins who may OWN a lead (DB-free).

Change Owner (super-admin popup) hands leads to an explicitly picked owner via
distribute_leads with auto=False; auto/sweep round-robin uses auto=True. The gate
in add_to_pool is `OWNER_ROLES if not auto else TELECALLER_ROLES` — an explicit
pick may land on an agency admin, an auto sweep may not. These pin that contract
so a later "simplify OWNER_ROLES back to TELECALLER_ROLES" regression fails here.

    python tests/test_owner_roles.py     # or: pytest tests/test_owner_roles.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SharedBackend", "src"))

from utils.constants import UserRole, TELECALLER_ROLES, OWNER_ROLES  # noqa: E402


def _allowed(role, auto):
    """Mirrors leadService.add_to_pool's role gate."""
    return role in (OWNER_ROLES if not auto else TELECALLER_ROLES)


def test_agency_admin_owns_only_on_explicit_pick():
    # Explicit pick (Change Owner): agency admins + telecallers may own.
    assert _allowed(UserRole.AGENCY_ADMIN, auto=False)
    assert _allowed(UserRole.TELECALLER, auto=False)
    assert _allowed(UserRole.AGENCY_TELECALLER, auto=False)
    # Auto/sweep round-robin: telecallers only, never agency admins.
    assert not _allowed(UserRole.AGENCY_ADMIN, auto=True)
    assert _allowed(UserRole.TELECALLER, auto=True)


def test_non_owner_roles_rejected_both_ways():
    for role in (UserRole.ACCOUNTANT, UserRole.DELIVERY_GUY, UserRole.OUTLET_MANAGER):
        assert not _allowed(role, auto=False)
        assert not _allowed(role, auto=True)


def test_owner_roles_is_telecallers_plus_agency_admin():
    assert set(TELECALLER_ROLES).issubset(set(OWNER_ROLES))
    assert UserRole.AGENCY_ADMIN in OWNER_ROLES
    assert UserRole.AGENCY_ADMIN not in TELECALLER_ROLES  # excluded from auto pool


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all owner_roles checks passed")
