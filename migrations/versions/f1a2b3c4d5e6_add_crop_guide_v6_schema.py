"""Add event_outbox, crop_reports, user_crop_reports, and
recommendation_history audit columns (spec §5 Phase 6) — additive only.
Column names/types copied verbatim from the spec's Phase 6 migration
block — farm_id, farm_name, livestock, current_crop_code, sowing_date.
(wizard_session_id is NOT one of these — per spec §3.3 item 4 it only
flows through the CropGuideCompleted broker event, Task 5; it is not a
recommendation_history column.)

Every operation is schema-qualified with SCHEMA, matching the other
migrations in this directory (see d3e4f5a6b7c8).

Revision ID: f1a2b3c4d5e6
Revises: d3e4f5a6b7c8
Create Date: 2026-10-14 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

from config import get_settings

revision = "f1a2b3c4d5e6"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None

SCHEMA = get_settings().name


def upgrade():
    op.add_column("recommendation_history", sa.Column("farm_id", sa.String(255), nullable=True), schema=SCHEMA)
    op.add_column("recommendation_history", sa.Column("farm_name", sa.String(255), nullable=True), schema=SCHEMA)
    op.add_column("recommendation_history", sa.Column("livestock", sa.String(255), nullable=True), schema=SCHEMA)
    op.add_column("recommendation_history", sa.Column("current_crop_code", sa.String(255), nullable=True), schema=SCHEMA)
    op.add_column("recommendation_history", sa.Column("sowing_date", sa.Date(), nullable=True), schema=SCHEMA)

    op.create_table(
        "event_outbox",
        sa.Column("uid", sa.String(), primary_key=True),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("event_key", sa.String(128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("idx_event_outbox_status", "event_outbox", ["status"], schema=SCHEMA)

    op.create_table(
        "crop_reports",
        sa.Column("uid", sa.String(), primary_key=True),
        sa.Column("crop_id", sa.String(64), nullable=False, unique=True),
        sa.Column("name_en", sa.String(128), nullable=False),
        sa.Column("name_local", sa.JSON(), nullable=False),
        sa.Column("family", sa.String(64), nullable=False),
        sa.Column("crop_type", sa.String(32), nullable=False),
        sa.Column("seasons", sa.JSON(), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=True),
        sa.Column("years_to_first_harvest", sa.Numeric(4, 2), nullable=True),
        sa.Column("suitable_soil_types", sa.JSON(), nullable=False),
        sa.Column("soil_texture", sa.JSON(), nullable=False),
        sa.Column("soil_drainage", sa.String(64), nullable=False),
        sa.Column("soil_ph_min", sa.Numeric(3, 1), nullable=False),
        sa.Column("soil_ph_max", sa.Numeric(3, 1), nullable=False),
        sa.Column("water_requirement", sa.String(32), nullable=False),
        sa.Column("cost_per_acre_inr", sa.Integer(), nullable=False),
        sa.Column("expected_yield_per_acre", sa.Numeric(10, 2), nullable=False),
        sa.Column("yield_unit", sa.String(32), nullable=False),
        sa.Column("profit_potential", sa.String(32), nullable=False),
        sa.Column("sowing_windows", sa.JSON(), nullable=False),
        sa.Column("profit_note", sa.Text(), nullable=False),
        sa.Column("weather", sa.JSON(), nullable=False),
        sa.Column("risks", sa.JSON(), nullable=False),
        sa.Column("tips", sa.JSON(), nullable=False),
        sa.Column("soil_economic_benefits", sa.JSON(), nullable=False),
        sa.Column("hero_image_url", sa.String(512), nullable=True),
        sa.Column("icon_url", sa.String(512), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("data_source", sa.Text(), nullable=True),
        sa.Column("review_status", sa.String(32), nullable=False, server_default="ai_draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("idx_crop_reports_family", "crop_reports", ["family"], schema=SCHEMA)

    op.create_table(
        "user_crop_reports",
        sa.Column("uid", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("farm_id", sa.String(64), nullable=True),
        sa.Column("recommendation_id", sa.String(64), nullable=False),
        sa.Column("farm_name", sa.String(128), nullable=True),
        sa.Column("season", sa.String(32), nullable=False),
        sa.Column("language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("top_crop_id", sa.String(64), nullable=False),
        sa.Column("top_crop_name", sa.String(128), nullable=False),
        sa.Column("top_crop_score", sa.Integer(), nullable=False),
        sa.Column("total_recommendations", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("report_data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("idx_user_crop_reports_user_id", "user_crop_reports", ["user_id"], schema=SCHEMA)
    op.create_index("idx_user_crop_reports_rec_id", "user_crop_reports", ["recommendation_id"], schema=SCHEMA)
    op.create_index("idx_user_crop_reports_farm_id", "user_crop_reports", ["farm_id"], schema=SCHEMA)


def downgrade():
    op.drop_table("user_crop_reports", schema=SCHEMA)
    op.drop_table("crop_reports", schema=SCHEMA)
    op.drop_table("event_outbox", schema=SCHEMA)
    op.drop_column("recommendation_history", "sowing_date", schema=SCHEMA)
    op.drop_column("recommendation_history", "current_crop_code", schema=SCHEMA)
    op.drop_column("recommendation_history", "livestock", schema=SCHEMA)
    op.drop_column("recommendation_history", "farm_name", schema=SCHEMA)
    op.drop_column("recommendation_history", "farm_id", schema=SCHEMA)
