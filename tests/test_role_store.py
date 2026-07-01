"""Guards the DB-backed role resolver (DB-free).

The roles tables are the runtime source of truth; ROLE_DEFINITIONS (code) is the seed +
fallback. These checks pin that contract: cache wins when present, code fills in when the
cache is empty or missing a role, and the wildcard short-circuits. Run::

    python tests/test_role_store.py      # or: pytest tests/test_role_store.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import path_setup  # noqa: E402,F401 — wires SharedBackend onto sys.path for services.roleStore

from utils import permissions  # noqa: E402
from utils.permissions import (ScopeLevel, WILDCARD, has_permission, role_perms,  # noqa: E402
                               role_scope_level, set_role_cache)
from utils.constants import UserRole  # noqa: E402
from services.roleStore import _roles_to_bootstrap  # noqa: E402


def _reset():
    set_role_cache({})   # back to code-dict fallback


def test_fallback_to_code_when_cache_empty():
    _reset()
    # Telecaller default perms come from ROLE_DEFINITIONS when nothing is loaded.
    assert "orders:read" in role_perms(UserRole.TELECALLER)
    assert role_scope_level(UserRole.CLUSTER_MANAGER) == ScopeLevel.CLUSTER


def test_db_cache_overrides_code():
    # A loaded role wins over the code dict — even narrowing an existing role's perms.
    set_role_cache({"TELECALLER": {"scope": ScopeLevel.OUTLET, "location_type": None,
                                   "perms": {"orders:read"}}})
    assert role_perms("TELECALLER") == {"orders:read"}
    assert has_permission("TELECALLER", "orders:read") is True
    assert has_permission("TELECALLER", "orders:write") is False   # pruned by the DB role
    _reset()


def test_custom_db_only_role_resolves():
    # A custom role that exists only in the DB (not in UserRole) resolves from the cache.
    set_role_cache({"REGIONAL_AUDITOR": {"scope": ScopeLevel.STATE, "location_type": None,
                                         "perms": {"reports:read", "orders:read"}}})
    assert role_scope_level("REGIONAL_AUDITOR") == ScopeLevel.STATE
    assert has_permission("REGIONAL_AUDITOR", "reports:read") is True
    _reset()


def test_wildcard_passes_everything():
    set_role_cache({"SUPER_ADMIN": {"scope": ScopeLevel.GLOBAL, "location_type": None,
                                    "perms": {WILDCARD}}})
    assert has_permission("SUPER_ADMIN", "anything:at:all") is True
    _reset()


def test_unknown_role_is_empty():
    _reset()
    assert role_perms("NOPE_NOT_A_ROLE") == set()
    assert has_permission("NOPE_NOT_A_ROLE", "orders:read") is False


def test_roles_to_bootstrap():
    # Missing roles (B, C) are the only ones bootstrapped; A already exists so it's excluded.
    assert _roles_to_bootstrap({"A", "B", "C"}, {"A"}) == {"B", "C"}
    # Nothing missing -> nothing re-seeded/overwritten once every defined role is present.
    assert _roles_to_bootstrap({"A", "B"}, {"A", "B", "C"}) == set()
    assert _roles_to_bootstrap(set(), set()) == set()


if __name__ == "__main__":
    test_fallback_to_code_when_cache_empty()
    test_db_cache_overrides_code()
    test_custom_db_only_role_resolves()
    test_wildcard_passes_everything()
    test_unknown_role_is_empty()
    test_roles_to_bootstrap()
    print("OK")
