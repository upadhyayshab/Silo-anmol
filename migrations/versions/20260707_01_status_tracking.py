"""Status-change tracking source + app_settings (daily auto-revert config)

Adds `delivery_tracking.source` ('erp' | 'rider_app' | 'system') so exports can show
which app last changed an order's status, and a generic `app_settings` key/value table
that holds the super-admin toggles for the daily job that reverts stale (non-terminal,
non-pending) orders back to PENDING. Config lives as ROWS (one per setting) — a new flag
is an insert, not a migration. Ships OFF: no rows means disabled.

Revision ID: 20260707_01_status_tracking
Revises: 20260704_01_selling_price
Create Date: 2026-07-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20260707_01_status_tracking"
down_revision = "20260704_01_selling_price"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("delivery_tracking", sa.Column("source", sa.String(20), nullable=True))
    op.create_table(
        "app_settings",
        sa.Column("uid", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
    )
    op.create_index("ix_app_settings_key", "app_settings", ["key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_app_settings_key", table_name="app_settings")
    op.drop_table("app_settings")
    op.drop_column("delivery_tracking", "source")
