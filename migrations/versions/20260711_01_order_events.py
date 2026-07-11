"""order_events — event_type + payload on delivery_tracking; sever FK+cascade so
the order lifecycle log survives hard delete. Backfill existing rows to STATUS_CHANGE.

Revision ID: 20260711_01_order_events
Revises: 20260711_01_attendance
Create Date: 2026-07-11 00:00:00.000000

Note: revision id kept <=32 chars. Chains off 20260711_01_attendance (the user's
in-flight attendance migration, current head) to keep the chain LINEAR — chaining
off tracker_inputs would create a second head. If the attendance revision id
changes, update down_revision to match.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260711_01_order_events"
down_revision = "20260711_01_attendance"
branch_labels = None
depends_on = None

# The FK constraint name Postgres auto-generates for delivery_tracking.order_id.
_FK = "delivery_tracking_order_id_fkey"


def upgrade() -> None:
    op.add_column("delivery_tracking", sa.Column("event_type", sa.String(32), nullable=True))
    op.add_column("delivery_tracking", sa.Column("payload", sa.JSON(), nullable=True))
    op.create_index("ix_delivery_tracking_event_type", "delivery_tracking", ["event_type"])

    # Backfill: every historical row is a status change (timeline display only; never
    # counts as an attempt — only RIDER_DISPOSITION rows do).
    op.execute("UPDATE delivery_tracking SET event_type = 'STATUS_CHANGE' WHERE event_type IS NULL")

    # Sever the FK so deleting an order no longer requires deleting its history.
    # IF EXISTS: name is the PG default but guard in case an env differs.
    op.execute(f"ALTER TABLE delivery_tracking DROP CONSTRAINT IF EXISTS {_FK}")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE delivery_tracking "
        f"ADD CONSTRAINT {_FK} FOREIGN KEY (order_id) REFERENCES customer_orders (uid)"
    )
    op.drop_index("ix_delivery_tracking_event_type", table_name="delivery_tracking")
    op.drop_column("delivery_tracking", "payload")
    op.drop_column("delivery_tracking", "event_type")
