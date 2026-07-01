"""
Unit tests for the roles-admin guardrails (utils/permissions.py) backing
routers/v1/roles.py — the superadmin-editable roles & permissions API.

Pure / DB-free: no FastAPI, no DB, no settings. Run with:
    python -m pytest tests/test_role_admin.py
or directly:
    python tests/test_role_admin.py
"""
import os
import sys

# The app runs with app/ as the import root (e.g. `from utils.constants import`).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from utils.permissions import (  # noqa: E402
    ScopeLevel, WILDCARD, ALL_PERMISSIONS,
    validate_role_perms, validate_scope_level, validate_new_role_name, diff_perms,
)


def test_validate_role_perms_accepts_valid_subset():
    validate_role_perms(["orders:read", "orders:write"])  # no raise


def test_validate_role_perms_accepts_empty():
    validate_role_perms([])  # no raise


def test_validate_role_perms_rejects_unknown_perm():
    try:
        validate_role_perms(["orders:read", "not:a:real:perm"])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_validate_role_perms_rejects_wildcard():
    try:
        validate_role_perms([WILDCARD])
        assert False, "expected ValueError"
    except ValueError:
        pass

    try:
        validate_role_perms(["orders:read", "*"])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_validate_role_perms_covers_all_permissions():
    # Sanity: the full vocabulary passes as a set (minus wildcard, which isn't in
    # ALL_PERMISSIONS anyway).
    validate_role_perms(list(ALL_PERMISSIONS))


def test_validate_scope_level_accepts_every_scopelevel_value():
    for scope in ScopeLevel:
        validate_scope_level(scope.value)  # no raise


def test_validate_scope_level_rejects_bad_string():
    try:
        validate_scope_level("NOT_A_SCOPE")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_validate_new_role_name_accepts_good_names():
    for name in ("REGIONAL_AUDITOR", "AB", "AB1", "A_B_C_9"):
        validate_new_role_name(name)  # no raise


def test_validate_new_role_name_rejects_bad_names():
    # "A" (single char) is rejected too: ROLE_NAME_RE requires a leading letter
    # PLUS 1-63 more chars, i.e. length 2-64.
    for name in ("lowercase", "1BAD", "has space", "", "_LEADING_UNDERSCORE",
                 "A", "a" * 65):
        try:
            validate_new_role_name(name)
            assert False, f"expected ValueError for {name!r}"
        except ValueError:
            pass


def test_diff_perms_computes_add_and_remove():
    desired = {"a", "b"}
    current = {"b", "c"}
    to_add, to_remove = diff_perms(desired, current)
    assert to_add == {"a"}
    assert to_remove == {"c"}


def test_diff_perms_no_change():
    same = {"a", "b"}
    to_add, to_remove = diff_perms(same, same)
    assert to_add == set()
    assert to_remove == set()


def test_diff_perms_empty_desired_removes_everything():
    to_add, to_remove = diff_perms(set(), {"a", "b"})
    assert to_add == set()
    assert to_remove == {"a", "b"}


if __name__ == "__main__":
    test_validate_role_perms_accepts_valid_subset()
    test_validate_role_perms_accepts_empty()
    test_validate_role_perms_rejects_unknown_perm()
    test_validate_role_perms_rejects_wildcard()
    test_validate_role_perms_covers_all_permissions()
    test_validate_scope_level_accepts_every_scopelevel_value()
    test_validate_scope_level_rejects_bad_string()
    test_validate_new_role_name_accepts_good_names()
    test_validate_new_role_name_rejects_bad_names()
    test_diff_perms_computes_add_and_remove()
    test_diff_perms_no_change()
    test_diff_perms_empty_desired_removes_everything()
    print("OK")
