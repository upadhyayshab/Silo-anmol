"""facebook_pages — connected Lead Ads pages (metadata only, no tokens)

Revision ID: 20260618_01_facebook_pages
Revises: 20260617_02_lead_source_outlet
Create Date: 2026-06-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260618_01_facebook_pages"
down_revision = "20260617_02_lead_source_outlet"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "facebook_pages",
        sa.Column("uid", sa.String(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column("page_name", sa.String(length=255), nullable=True),
        sa.Column("is_subscribed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("routing_state", sa.String(length=128), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_backfill_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("uid"),
        sa.UniqueConstraint("page_id"),
    )
    op.create_index("ix_facebook_pages_page_id", "facebook_pages", ["page_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_facebook_pages_page_id", table_name="facebook_pages")
    op.drop_table("facebook_pages")
