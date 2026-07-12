"""lead_esc_activity — add order_escalated / order_verification to the lead_activity_type
native enum, so an order's CRM escalation and the CRM verification decision show as their
own distinct entries on the lead's activity timeline (not lumped into order_update).

Revision ID: 20260712_02_lead_esc_activity
Revises: 20260712_01_collection_cb
Create Date: 2026-07-12 00:00:00.000000

Note: revision id kept <=32 chars. ALTER TYPE ADD VALUE must run outside a transaction,
so it uses an autocommit block (same pattern as prior enum-value migrations).
"""
from alembic import op

revision = "20260712_02_lead_esc_activity"
down_revision = "20260712_01_collection_cb"
branch_labels = None
depends_on = None

_NEW_VALUES = ("order_escalated", "order_verification")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        for val in _NEW_VALUES:
            op.execute(f"ALTER TYPE lead_activity_type ADD VALUE IF NOT EXISTS '{val}'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; leaving the values in place is harmless
    # (nothing references them once the app stops writing them). No-op downgrade.
    pass
