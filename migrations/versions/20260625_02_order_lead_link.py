"""Link a customer order back to its CRM lead

Adds ``customer_orders.lead_id`` — set when a telecaller places an order from a
lead's "Place Order" in the CRM (checkpoint 3.5). This makes orders-by-lead a
first-class query (reporting / future FTU auto-advance), and an ``order`` value
is added to the ``lead_activity_type`` enum so the placement shows on the lead
timeline.

``customer_orders`` is a core table present in every environment, so the column
is always added. The CRM ``leads`` table and the ``lead_activity_type`` enum
only exist where the native CRM was deployed (the migration graph diverges —
CRM is excluded from some environments), so the FK constraint and the enum value
are applied only when their targets exist. The column stays harmlessly NULL
elsewhere.

Revision ID: 20260625_02_order_lead_link
Revises: 20260625_01_transfer_source_not_null
Create Date: 2026-06-25 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20260625_02_order_lead_link"
down_revision = "20260625_01_transfer_src"
branch_labels = None
depends_on = None

FK_NAME = "fk_customer_orders_lead_id"
IX_NAME = "ix_customer_orders_lead_id"


def upgrade() -> None:
    op.add_column("customer_orders", sa.Column("lead_id", sa.String(), nullable=True))
    op.create_index(IX_NAME, "customer_orders", ["lead_id"])

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # sqlite (tests) stores enums as text and is lax about FKs — column is enough.
        return

    leads_exists = bind.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_name = 'leads'")
    ).scalar()
    if leads_exists:
        op.create_foreign_key(
            FK_NAME, "customer_orders", "leads", ["lead_id"], ["uid"],
        )

    enum_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'lead_activity_type'")
    ).scalar()
    if enum_exists:
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block; step
        # outside Alembic's transaction. IF NOT EXISTS keeps it idempotent.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE lead_activity_type ADD VALUE IF NOT EXISTS 'order'")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        fk_exists = bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.table_constraints "
                "WHERE constraint_name = :n AND table_name = 'customer_orders'"
            ),
            {"n": FK_NAME},
        ).scalar()
        if fk_exists:
            op.drop_constraint(FK_NAME, "customer_orders", type_="foreignkey")

    op.drop_index(IX_NAME, table_name="customer_orders")
    op.drop_column("customer_orders", "lead_id")
    # The 'order' enum value is intentionally left in place: PostgreSQL cannot
    # drop a single enum value without recreating the type, and a spare value is
    # harmless (mirrors the lead_source 'Outlet' downgrade).
