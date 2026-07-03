"""Saved lead segments — named, reusable advanced-filter trees

Adds ``lead_segments`` — one row per saved segment (a named advanced-filter tree
the CRM can re-run). Owned by its creator (``owner_user_id`` -> users.uid);
``is_shared`` exposes it to everyone; ``surface`` marks where it applies. Additive
and CRM-adjacent (it only references ``users``), so it's safe across the divergent
migration graphs. Mirrors 20260628_01_telecaller_status.

Revision ID: 20260703_01_lead_segments
Revises: 20260702_04_src_telecaller
Create Date: 2026-07-03 00:00:00.000000

Note: revision id kept <=32 chars to fit alembic_version.version_num (varchar 32).
"""
from alembic import op
import sqlalchemy as sa


revision = "20260703_01_lead_segments"
down_revision = "20260702_04_src_telecaller"
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
        "lead_segments",
        *_base_columns(),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("filter", sa.JSON(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=True),
        sa.Column("is_shared", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("surface", sa.String(length=16), server_default="both", nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.uid"]),
        sa.PrimaryKeyConstraint("uid"),
    )
    op.create_index("ix_lead_segments_owner_user_id", "lead_segments", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_lead_segments_owner_user_id", table_name="lead_segments")
    op.drop_table("lead_segments")
