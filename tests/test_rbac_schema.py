"""DB-shape + contract guards for the RBAC rollout (Steps 1-3).

Pins the schema changes the migrations made, without needing a live DB — just imports the
ORM schemas and reflects their columns/constraints:

  * Step 1: `roles` + `role_permissions` tables exist with their key columns, and a role
    cannot list the same permission twice (unique(role_name, permission)).
  * Step 2: `users.role` is a plain varchar, NOT a Postgres enum (so unknown/custom role
    names read back as strings).
  * Step 3: scope grants are limited to STATE/CLUSTER/AGENCY (OUTLET = the user's own
    outlet_id; GLOBAL needs no grant) — guards against a scope-escape via a widened set.

`import path_setup` wires SharedBackend (mirrors tests/test_agency.py); schema/router
imports stay inside the tests. Run::

    python tests/test_rbac_schema.py     # or: pytest tests/test_rbac_schema.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import path_setup  # noqa: F401,E402  — must precede manager imports


def test_roles_tables_shape():
    from managers import RoleSchema, RolePermissionSchema
    assert RoleSchema.__tablename__ == "roles"
    for c in ("uid", "name", "scope_level", "location_type", "is_system", "description"):
        assert c in RoleSchema.__table__.c, f"roles.{c} missing"

    assert RolePermissionSchema.__tablename__ == "role_permissions"
    for c in ("uid", "role_name", "permission"):
        assert c in RolePermissionSchema.__table__.c, f"role_permissions.{c} missing"

    # Deliberate: a role can't carry the same permission twice.
    uniques = {
        tuple(sorted(col.name for col in con.columns))
        for con in RolePermissionSchema.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("permission", "role_name") in uniques, \
        f"missing unique(role_name, permission); have: {uniques}"


def test_user_role_is_varchar_not_enum():
    import sqlalchemy as sa
    from managers import UserSchema
    col = UserSchema.__table__.c.role
    # sa.Enum subclasses String, so assert it's String AND specifically not an Enum.
    assert isinstance(col.type, sa.String), f"users.role is not a String type: {col.type!r}"
    assert not isinstance(col.type, sa.Enum), "users.role should be plain varchar, not an Enum"


def test_scope_grants_limited_to_state_cluster_agency():
    from routers.v1.users import _ASSIGNABLE_LEVELS
    assert _ASSIGNABLE_LEVELS == {"STATE", "CLUSTER", "AGENCY"}
    assert "OUTLET" not in _ASSIGNABLE_LEVELS    # OUTLET scope = the user's own outlet_id
    assert "GLOBAL" not in _ASSIGNABLE_LEVELS    # GLOBAL roles need no grant


if __name__ == "__main__":
    test_roles_tables_shape()
    test_user_role_is_varchar_not_enum()
    test_scope_grants_limited_to_state_cluster_agency()
    print("OK")
