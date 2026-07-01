"""Add `taluk` to leads (telecaller geo auto-fill persists the chosen taluka).

Dev/CRM only — do not ride onto the prod chain.

Revision ID: 20260630_02_lead_taluk
Revises: 20260630_01_pincodes
"""
from alembic import op
import sqlalchemy as sa

revision = "20260630_02_lead_taluk"
down_revision = "20260630_01_pincodes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("taluk", sa.String(length=100), nullable=True))
    op.create_index("ix_leads_taluk", "leads", ["taluk"])
    # Backfill: existing leads were free-text entry. Canonicalize geography to
    # lowercase so they join cleanly to cluster_districts. (taluk is freshly added,
    # all NULL.) Not reversed on downgrade — lowercasing is lossy and harmless.
    op.execute(
        "UPDATE leads SET state = lower(state), district = lower(district) "
        "WHERE state IS NOT NULL OR district IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_leads_taluk", table_name="leads")
    op.drop_column("leads", "taluk")
