"""fb_leadgen_forms + fb_field_mappings — configurable Meta->lead field mapping

Revision ID: 20260622_01_fb_mapping
Revises: 20260618_01_facebook_pages
Create Date: 2026-06-22 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260622_01_fb_mapping"
down_revision = "20260618_01_facebook_pages"
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
        "fb_leadgen_forms",
        *_base_columns(),
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column("form_id", sa.String(length=64), nullable=False),
        sa.Column("form_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("questions", sa.JSON(), nullable=True),
        sa.Column("fb_created_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("uid"),
        sa.UniqueConstraint("form_id"),
    )
    op.create_index("ix_fb_leadgen_forms_page_id", "fb_leadgen_forms", ["page_id"])
    op.create_index("ix_fb_leadgen_forms_status", "fb_leadgen_forms", ["status"])

    op.create_table(
        "fb_field_mappings",
        *_base_columns(),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="default"),
        sa.Column("form_id", sa.String(length=64), nullable=True),
        sa.Column("field_kind", sa.String(length=16), nullable=False, server_default="marketing"),
        sa.Column("meta_field", sa.String(length=255), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=True),
        sa.Column("target_kind", sa.String(length=16), nullable=False, server_default="campaign_data"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("uid"),
    )
    op.create_index("ix_fb_field_mappings_scope", "fb_field_mappings", ["scope"])
    op.create_index("ix_fb_field_mappings_form_id", "fb_field_mappings", ["form_id"])
    # Partial unique indexes: Postgres NULLs are distinct, so split default vs per-form.
    op.create_index("uq_fb_mapping_default", "fb_field_mappings",
                    ["scope", "field_kind", "meta_field"],
                    unique=True, postgresql_where=sa.text("form_id IS NULL"))
    op.create_index("uq_fb_mapping_form", "fb_field_mappings",
                    ["scope", "form_id", "field_kind", "meta_field"],
                    unique=True, postgresql_where=sa.text("form_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("uq_fb_mapping_form", table_name="fb_field_mappings")
    op.drop_index("uq_fb_mapping_default", table_name="fb_field_mappings")
    op.drop_index("ix_fb_field_mappings_form_id", table_name="fb_field_mappings")
    op.drop_index("ix_fb_field_mappings_scope", table_name="fb_field_mappings")
    op.drop_table("fb_field_mappings")
    op.drop_index("ix_fb_leadgen_forms_status", table_name="fb_leadgen_forms")
    op.drop_index("ix_fb_leadgen_forms_page_id", table_name="fb_leadgen_forms")
    op.drop_table("fb_leadgen_forms")
