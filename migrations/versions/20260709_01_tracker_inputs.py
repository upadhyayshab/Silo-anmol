"""daily_tracker_inputs — hand-entered metrics for the Business Daily Tracker.

Revision ID: 20260709_01_tracker_inputs
Revises: 20260707_01_status_tracking
Create Date: 2026-07-09 00:00:00.000000

Note: revision id kept <=32 chars to fit alembic_version.version_num (varchar 32).
"""
from alembic import op
import sqlalchemy as sa


revision = "20260709_01_tracker_inputs"
down_revision = "20260707_01_status_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_tracker_inputs",
        sa.Column("uid", sa.String(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tracker_date", sa.Date(), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=False),
        sa.Column("metric_key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Numeric(14, 2), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("uid"),
        # Postgres backs this with a composite b-tree; it is the only index this
        # table needs. Standalone tracker_date / region indexes would be redundant.
        sa.UniqueConstraint("tracker_date", "region", "metric_key", name="uq_tracker_input"),
    )


def downgrade() -> None:
    op.drop_table("daily_tracker_inputs")
