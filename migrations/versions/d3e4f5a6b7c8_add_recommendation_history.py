"""add recommendation_history table

Revision ID: d3e4f5a6b7c8
Revises: c7d8e9f0a1b2
Create Date: 2026-08-17

Audit log of every /recommend call: request params, soil-texture detection
result, and the full ranked results, keyed by the caller's upid and created_at.
See app/managers/cropManagers.py::RecommendationHistorySchema.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from config import get_settings

# revision identifiers, used by Alembic.
revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, None] = 'c7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = get_settings().name


def _base_columns():
    return [
        sa.Column("uid", sa.String, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "recommendation_history",
        *_base_columns(),
        sa.Column("upid", sa.String, nullable=False),
        sa.Column("subscription_id", sa.String, nullable=False),
        sa.Column("lat", sa.Float, nullable=True),
        sa.Column("lon", sa.Float, nullable=True),
        sa.Column("season", postgresql.ENUM("KHARIF", "RABI", "ZAID", "LONGTERM", name="season_enum", create_type=False),
                  nullable=False),
        sa.Column("water_methods", sa.JSON, nullable=False),
        sa.Column("budget_bucket", postgresql.ENUM("B1", "B2", "B3", "B4", name="budget_bucket_enum", create_type=False),
                  nullable=False),
        sa.Column("previous_crop_code", sa.String, nullable=True),
        sa.Column("was_fallow", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("top_n", sa.Integer, nullable=False),
        sa.Column("lang", sa.String(5), nullable=False),
        sa.Column("zone_filter_applied", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("soil_class_detected", sa.Integer, nullable=True),
        sa.Column("soil_texture_label", sa.String, nullable=True),
        sa.Column("results", sa.JSON, nullable=False),
        sa.Column("notes", sa.JSON, nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_recommendation_history_upid", "recommendation_history", ["upid"], schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_recommendation_history_upid", table_name="recommendation_history", schema=SCHEMA)
    op.drop_table("recommendation_history", schema=SCHEMA)
