"""attendance_days — per-telecaller per-IST-day working span for the attendance dashboard.

Revision ID: 20260711_01_attendance
Revises: 20260709_01_tracker_inputs
Create Date: 2026-07-11 00:00:00.000000

Note: revision id kept <=32 chars to fit alembic_version.version_num (varchar 32).
"""
from alembic import op
import sqlalchemy as sa


revision = "20260711_01_attendance"
down_revision = "20260709_01_tracker_inputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attendance_days",
        sa.Column("uid", sa.String(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.uid"]),
        sa.PrimaryKeyConstraint("uid"),
        # Composite unique is the upsert conflict target; Postgres backs it with a b-tree
        # that also serves per-user lookups.
        sa.UniqueConstraint("user_id", "work_date", name="uq_attendance_user_day"),
    )
    # Month scans read every agent's rows for a date window -> index work_date.
    op.create_index("ix_attendance_days_work_date", "attendance_days", ["work_date"])
    op.create_index("ix_attendance_days_user_id", "attendance_days", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_attendance_days_user_id", table_name="attendance_days")
    op.drop_index("ix_attendance_days_work_date", table_name="attendance_days")
    op.drop_table("attendance_days")
