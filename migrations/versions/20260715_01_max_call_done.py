"""Add 'Max Call Done' to the lead_stage enum

New stage for the "Max Call Attempts (20 calls)" sub-disposition (Task A) — a UI-gated
MANUAL pick offered only once a lead already has >=20 Not-Connected calls logged, never
an automatic count-triggered flip. `leads.stage` is a native Postgres enum (`lead_stage`),
so the new value must be registered on the type or the stage-change write fails at runtime.

The `lead_stage` enum only exists where the native CRM was deployed (the migration graph
diverges — CRM is excluded from some environments), so the value is added only when the
type is present. Mirrors 20260702_02_reengaged_activity.py's `re_engaged` value addition.

Revision ID: 20260715_01_max_call_done
Revises: 20260712_02_lead_esc_activity
Create Date: 2026-07-15 00:00:00.000000

Note: revision id kept <=32 chars (a prior 34-char id bricked prod). down_revision
chains off 20260712_02_lead_esc_activity, the ACTUAL current head as of 2026-07-15 —
two migrations (20260712_01_collection_cb, 20260712_02_lead_esc_activity) landed after
20260711_01_order_events, which is now stale. Chaining off the stale revision would fork
the graph into two heads and break `alembic upgrade head`.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260715_01_max_call_done"
down_revision = "20260712_02_lead_esc_activity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # sqlite (tests) stores enums as text — nothing to alter.
        return

    enum_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'lead_stage'")
    ).scalar()
    if enum_exists:
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block; step outside
        # Alembic's transaction. IF NOT EXISTS keeps it idempotent.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE lead_stage ADD VALUE IF NOT EXISTS 'Max Call Done'")


def downgrade() -> None:
    # The 'Max Call Done' enum value is intentionally left in place: PostgreSQL cannot
    # drop a single enum value without recreating the type, and a spare value is
    # harmless (mirrors the 're_engaged' / 'order' / 'order_update' downgrades).
    pass
