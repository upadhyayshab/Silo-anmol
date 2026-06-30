"""CRM-ERP integration columns left out of the model-only changes

Adds the columns/enum value the CRM-ERP integration code already references but
that no migration created (so the ORM model and the live Postgres schema had
diverged — login broke on ``users.last_active_at does not exist``):

  * ``users.last_active_at``      — presence tracking for lead distribution
  * ``users.assignment_quota``    — admin-configurable active-lead cap
  * ``customer_orders.source``    — order source (daily report + lead origin)
  * ``lead_activity_type`` value ``order_update`` — status/payment timeline events

``users`` and ``customer_orders`` are core tables present in every environment,
so the columns are always added. The ``lead_activity_type`` enum only exists
where the native CRM was deployed (the migration graph diverges — CRM is
excluded from some environments), so the new value is added only when the type
exists. Mirrors 20260625_02_order_lead_link.

Revision ID: 20260628_02_crm_cols
Revises: 20260628_01_telecaller_status
Create Date: 2026-06-28 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20260628_02_crm_cols"  # <=32 chars: alembic_version is VARCHAR(32)
down_revision = "20260628_01_telecaller_status"
branch_labels = None
depends_on = None

IX_SOURCE = "ix_customer_orders_source"


def upgrade() -> None:
    op.add_column("users", sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("assignment_quota", sa.Integer(), nullable=True))

    op.add_column("customer_orders", sa.Column("source", sa.String(length=50), nullable=True))
    op.create_index(IX_SOURCE, "customer_orders", ["source"])

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    enum_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'lead_activity_type'")
    ).scalar()
    if enum_exists:
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block; step
        # outside Alembic's transaction. IF NOT EXISTS keeps it idempotent.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE lead_activity_type ADD VALUE IF NOT EXISTS 'order_update'")


def downgrade() -> None:
    op.drop_index(IX_SOURCE, table_name="customer_orders")
    op.drop_column("customer_orders", "source")
    op.drop_column("users", "assignment_quota")
    op.drop_column("users", "last_active_at")
    # The 'order_update' enum value is intentionally left in place: PostgreSQL
    # cannot drop a single enum value without recreating the type, and a spare
    # value is harmless (mirrors 20260625_02_order_lead_link).
