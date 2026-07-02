"""Add 'medusa' value to the lead_source enum

Leads created by the D2C webstore (Medusa) carry Source='medusa' in LeadSquared
(~115 leads at backfill time). The value must exist on the Postgres `lead_source`
type before the LSQ backfill can insert them with a real source instead of NULL.

Revision ID: 20260702_03_lead_source_medusa
Revises: 20260702_02_reengaged
Create Date: 2026-07-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20260702_03_lead_source_medusa"
down_revision = "20260702_02_reengaged"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Other dialects (e.g. sqlite in tests) store the enum as plain text.
        return
    type_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'lead_source'")
    ).scalar()
    if not type_exists:
        return
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block, so step
    # outside Alembic's transaction. IF NOT EXISTS keeps it idempotent.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE lead_source ADD VALUE IF NOT EXISTS 'medusa'")


def downgrade() -> None:
    # Postgres can't drop a single enum value without rewriting the type;
    # leaving it in place is harmless, so downgrade is a no-op.
    pass
