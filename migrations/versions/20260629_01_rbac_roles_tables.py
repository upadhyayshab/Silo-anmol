"""DB-backed RBAC role store: `roles` + `role_permissions` tables.

Additive only — creates the two tables that hold roles as data (so admins can compose
custom roles without a deploy). Does NOT touch `users.role` yet; that enum→varchar FK
conversion is a separate, gated migration. The 25 default roles are seeded into these
tables at app startup (`services.roleStore.seed_default_roles`), not here.

Chained on `20260628_02_crm_cols`. As of 2026-07-02 CRM/FB is deployed to prod, so no
re-parenting is needed — that ancestor is in prod's chain (CRM/prod separation retired).

Revision ID: 20260629_01_rbac_roles_tables
Revises: 20260628_02_crm_cols
"""
from alembic import op
import sqlalchemy as sa

revision = "20260629_01_rbac_roles_tables"
down_revision = "20260628_02_crm_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Idempotent guards — shared dev DB where create_all may have run ahead of alembic.
    if not insp.has_table("roles"):
        op.create_table(
            "roles",
            sa.Column("uid", sa.String(), primary_key=True),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("scope_level", sa.String(16), nullable=False),
            sa.Column("location_type", sa.String(32), nullable=True),
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("description", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("name", name="uq_roles_name"),
        )
        op.create_index("ix_roles_name", "roles", ["name"])

    if not insp.has_table("role_permissions"):
        op.create_table(
            "role_permissions",
            sa.Column("uid", sa.String(), primary_key=True),
            sa.Column("role_name", sa.String(64), sa.ForeignKey("roles.name"), nullable=False),
            sa.Column("permission", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("role_name", "permission", name="uq_role_permission"),
        )
        op.create_index("ix_role_permissions_role_name", "role_permissions", ["role_name"])


def downgrade() -> None:
    op.drop_table("role_permissions")
    op.drop_table("roles")
