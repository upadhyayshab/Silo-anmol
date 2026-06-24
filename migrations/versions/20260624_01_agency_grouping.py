"""Agency grouping: agencies table, users.agency_id, customer_orders.agency_id,
AGENCY_TELECALLER + AGENCY_ADMIN role enum values.

ponytail: chained on the committed RBAC Phase 2 migration (NOT the uncommitted
FB chain) so RBAC stays self-consistent in git and prod-independent of CRM/FB.
This leaves a 2nd alembic head on dev disk alongside the untracked FB chain —
reconcile with a merge migration once the FB work is committed.

Revision ID: 20260624_01_agency_grouping
Revises: 20260623_01_rbac_roles_scope
"""
from alembic import op
import sqlalchemy as sa

revision = "20260624_01_agency_grouping"
down_revision = "20260623_01_rbac_roles_scope"
branch_labels = None
depends_on = None

NEW_ROLES = ["AGENCY_TELECALLER", "AGENCY_ADMIN"]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    # Idempotent guards — shared dev DB where create_all may have run ahead of alembic.
    if not insp.has_table("agencies"):
        op.create_table(
            "agencies",
            sa.Column("uid", sa.String(), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "agency_id" not in {c["name"] for c in insp.get_columns("users")}:
        op.add_column("users", sa.Column("agency_id", sa.String(), sa.ForeignKey("agencies.uid"), nullable=True))
    if "ix_users_agency_id" not in {i["name"] for i in insp.get_indexes("users")}:
        op.create_index("ix_users_agency_id", "users", ["agency_id"])
    if "agency_id" not in {c["name"] for c in insp.get_columns("customer_orders")}:
        op.add_column("customer_orders", sa.Column("agency_id", sa.String(), sa.ForeignKey("agencies.uid"), nullable=True))
    if "ix_customer_orders_agency_id" not in {i["name"] for i in insp.get_indexes("customer_orders")}:
        op.create_index("ix_customer_orders_agency_id", "customer_orders", ["agency_id"])

    if bind.dialect.name != "postgresql":
        return
    # Scope to current_schema() — sibling services share this DB with their own
    # users.role enums; an unscoped match would alter the wrong type.
    type_name = bind.execute(sa.text("""
        SELECT t.typname FROM pg_type t
        JOIN pg_attribute a ON a.atttypid = t.oid
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'users' AND a.attname = 'role' AND a.attnum > 0
          AND n.nspname = current_schema()
    """)).scalar()
    if not type_name:
        return
    with op.get_context().autocommit_block():
        for role in NEW_ROLES:
            op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{role}'")


def downgrade() -> None:
    op.drop_index("ix_customer_orders_agency_id", "customer_orders")
    op.drop_column("customer_orders", "agency_id")
    op.drop_index("ix_users_agency_id", "users")
    op.drop_column("users", "agency_id")
    op.drop_table("agencies")
