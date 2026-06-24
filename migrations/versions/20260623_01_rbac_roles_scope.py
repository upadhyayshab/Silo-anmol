"""RBAC: new role enum values + user_scope_assignments table

Registers the blueprint roles on the `users.role` Postgres enum and adds a
multi-valued scope table so one user can manage several clusters/states.

ponytail: chained on the dev head (fb_mapping). Prod's migration graph diverges
(CRM/FB excluded) — rebase this revision's down_revision when porting to prod.

Revision ID: 20260623_01_rbac_roles_scope
Revises: 20260622_01_fb_mapping
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa


revision = "20260623_01_rbac_roles_scope"
down_revision = "20260622_01_fb_mapping"
branch_labels = None
depends_on = None

# Snapshot of the role names added in this revision (kept literal — migrations
# must not import the app enum, which keeps changing).
NEW_ROLES = [
    "VIEWER", "CLUSTER_MANAGER", "STATE_HEAD", "MARKETING_EXECUTIVE",
    "MARKETING_HEAD", "FINANCE_LEAD", "AUDITOR", "CEO", "CFO", "COO", "CGO",
    "USER_ADMIN", "CATALOGUE_ADMIN", "CONFIG_ADMIN", "OPS_ADMIN", "FINANCE_ADMIN",
]


def upgrade() -> None:
    op.create_table(
        "user_scope_assignments",
        sa.Column("uid", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.uid"), nullable=False, index=True),
        sa.Column("scope_level", sa.String(), nullable=False),   # CLUSTER | STATE | OUTLET
        sa.Column("scope_value", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "scope_level", "scope_value", name="uq_user_scope"),
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # sqlite (tests) stores the enum as plain text — nothing to alter

    # Discover the actual enum type backing users.role rather than guessing its name.
    type_name = bind.execute(sa.text("""
        SELECT t.typname
        FROM pg_type t
        JOIN pg_attribute a ON a.atttypid = t.oid
        JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = 'users' AND a.attname = 'role' AND a.attnum > 0
    """)).scalar()
    if not type_name:
        return  # role column isn't a native enum in this environment

    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block; step outside.
    # IF NOT EXISTS keeps it idempotent / safe to re-run.
    with op.get_context().autocommit_block():
        for role in NEW_ROLES:
            op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{role}'")


def downgrade() -> None:
    op.drop_table("user_scope_assignments")
    # Postgres has no native single-value enum drop; leaving the values is harmless.
