"""Add state (region) column to users for CRM lead routing

Revision ID: 20260616_02_user_state
Revises: 20260616_01_crm_lead_tables
Create Date: 2026-06-16 00:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20260616_02_user_state"
down_revision = "20260616_01_crm_lead_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("state", sa.String(length=100), nullable=True))
    op.create_index("ix_users_state", "users", ["state"])
    # Backfill: a user's region defaults to their outlet's state.
    op.execute(
        """
        UPDATE users u
        SET state = o.state
        FROM outlets o
        WHERE u.outlet_id = o.uid
          AND u.state IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_users_state", table_name="users")
    op.drop_column("users", "state")
