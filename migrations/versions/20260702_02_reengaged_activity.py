"""Add ``re_engaged`` to the lead_activity_type enum

The re-engagement flow (a dormant lead re-submitting a form) writes a
``RE_ENGAGED`` timeline entry via ``record_activity``. ``lead_activities``
.``activity_type`` is a native Postgres enum (``lead_activity_type``), so the
new value must be registered on the type or the INSERT fails at runtime.

The ``lead_activity_type`` enum only exists where the native CRM was deployed
(the migration graph diverges — CRM is excluded from some environments), so the
value is added only when the type is present. Mirrors the ``order`` /
``order_update`` value additions.

Revision ID: 20260702_02_reengaged
Revises: 20260702_01_rename_attribution
Create Date: 2026-07-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20260702_02_reengaged"
down_revision = "20260702_01_rename_attribution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # sqlite (tests) stores enums as text — nothing to alter.
        return

    enum_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'lead_activity_type'")
    ).scalar()
    if enum_exists:
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block; step
        # outside Alembic's transaction. IF NOT EXISTS keeps it idempotent.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE lead_activity_type ADD VALUE IF NOT EXISTS 're_engaged'")


def downgrade() -> None:
    # The 're_engaged' enum value is intentionally left in place: PostgreSQL
    # cannot drop a single enum value without recreating the type, and a spare
    # value is harmless (mirrors the 'order' / 'order_update' downgrades).
    pass
