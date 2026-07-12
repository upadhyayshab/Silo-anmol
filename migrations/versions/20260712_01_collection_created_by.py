"""Add created_by to outlet_daily_collections (records who logged the collection)

Revision ID: 20260712_01_collection_cb
Revises: 20260711_01_order_events
Create Date: 2026-07-12 00:00:00.000000

NOTE: outlet_daily_collections was created outside Alembic (no create-table
migration exists), so this migration is written idempotently — it can run whether
or not the column was already added by hand on an environment.
"""
from alembic import op


revision = "20260712_01_collection_cb"
down_revision = "20260711_01_order_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE outlet_daily_collections "
        "ADD COLUMN IF NOT EXISTS created_by VARCHAR"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_outlet_daily_collections_created_by "
        "ON outlet_daily_collections (created_by)"
    )
    # FK to users.uid, guarded so re-runs don't error.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_outlet_daily_collections_created_by'
            ) THEN
                ALTER TABLE outlet_daily_collections
                ADD CONSTRAINT fk_outlet_daily_collections_created_by
                FOREIGN KEY (created_by) REFERENCES users (uid);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE outlet_daily_collections "
        "DROP CONSTRAINT IF EXISTS fk_outlet_daily_collections_created_by"
    )
    op.execute("DROP INDEX IF EXISTS ix_outlet_daily_collections_created_by")
    op.execute(
        "ALTER TABLE outlet_daily_collections DROP COLUMN IF EXISTS created_by"
    )
