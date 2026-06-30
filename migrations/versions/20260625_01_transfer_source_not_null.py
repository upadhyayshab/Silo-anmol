"""Enforce non-NULL source on stock transfers at the DB level

Source (`from_outlet_id`) is now mandatory for every stock transfer — the API
already rejects null/empty, but a CHECK constraint guarantees it for ANY path
(scripts, raw SQL, future code), not just the application layer.

Added as NOT VALID: new inserts/updates are checked, but pre-existing
NULL-source rows (created before source became mandatory) are grandfathered so
the migration cannot fail on legacy data. `to_outlet_id` is already NOT NULL.

Revision ID: 20260625_01_transfer_source_not_null
Revises: 20260625_01_merge_heads
Create Date: 2026-06-25 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20260625_01_transfer_src"  # <=32 chars: alembic_version is VARCHAR(32)
down_revision = "20260625_01_merge_heads"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_transfers_from_outlet_not_null"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Other dialects (e.g. sqlite in tests) don't support NOT VALID; the
        # application-layer validation covers them.
        return
    exists = bind.execute(
        sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": _CONSTRAINT}
    ).scalar()
    if exists:
        return
    op.execute(
        "ALTER TABLE stock_transfer_orders "
        f"ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (from_outlet_id IS NOT NULL) NOT VALID"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"ALTER TABLE stock_transfer_orders DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
