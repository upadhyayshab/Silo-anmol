"""Telecaller presence for inbound routing (Feature 3.3)

Adds ``telecaller_status`` — one row per telecaller, upserted by the frontend
presence heartbeat. Inbound routing (3.2) rings only telecallers whose heartbeat
is fresh, so this table is the liveness source. Additive and CRM-only (it only
references ``users``), so it's safe across the divergent migration graphs.

Revision ID: 20260628_01_telecaller_status
Revises: 20260625_02_order_lead_link
Create Date: 2026-06-28 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20260628_01_telecaller_status"
down_revision = "20260625_02_order_lead_link"
branch_labels = None
depends_on = None


def _base_columns():
    return [
        sa.Column("uid", sa.String(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "telecaller_status",
        *_base_columns(),
        sa.Column("telecaller_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="available", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["telecaller_id"], ["users.uid"]),
        sa.PrimaryKeyConstraint("uid"),
        sa.UniqueConstraint("telecaller_id", name="uq_telecaller_status_telecaller_id"),
    )
    op.create_index("ix_telecaller_status_telecaller_id", "telecaller_status", ["telecaller_id"])


def downgrade() -> None:
    op.drop_index("ix_telecaller_status_telecaller_id", table_name="telecaller_status")
    op.drop_table("telecaller_status")
