"""Add week_start to inventory_audits (ISO-week keying + weekly uniqueness)

Revision ID: 20260616_01_audit_week_start
Revises: 692d425f260e
Create Date: 2026-06-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20260616_01_audit_week_start'
down_revision: Union[str, None] = '692d425f260e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0. Add the CLOSED status used by the Wednesday auto-close. ALTER TYPE ... ADD
    #    VALUE cannot run inside a transaction, so use an autocommit block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE auditstatus ADD VALUE IF NOT EXISTS 'CLOSED'")

    # 1. Add the column nullable so we can backfill existing rows.
    op.add_column('inventory_audits', sa.Column('week_start', sa.Date(), nullable=True))

    # 2. Backfill: the Saturday on/before audit_date — the start of the audit cycle
    #    (generated Saturday, closes the following Wednesday). ISODOW: Mon=1..Sun=7,
    #    Sat=6, so ((isodow + 1) % 7) days back lands on that Saturday.
    op.execute(
        "UPDATE inventory_audits "
        "SET week_start = (audit_date - ((EXTRACT(ISODOW FROM audit_date)::int + 1) % 7) * INTERVAL '1 day')::date "
        "WHERE week_start IS NULL"
    )

    # 3. Collapse pre-existing duplicates. The old dedup was per audit_date, so an
    #    outlet could have several audits in one week. Keep one per (outlet, week) —
    #    preferring a COMPLETED row, then the most recent — and soft-delete the rest
    #    so the partial unique index below can be created.
    op.execute(
        """
        WITH ranked AS (
            SELECT uid,
                   row_number() OVER (
                       PARTITION BY outlet_id, week_start
                       ORDER BY (status = 'COMPLETED') DESC, created_at DESC, uid
                   ) AS rn
            FROM inventory_audits
            WHERE deleted_at IS NULL
        )
        UPDATE inventory_audits a
        SET deleted_at = now()
        FROM ranked r
        WHERE a.uid = r.uid AND r.rn > 1
        """
    )

    # 4. Enforce NOT NULL now that every row has a value.
    op.alter_column('inventory_audits', 'week_start', nullable=False)

    # 5. Index for week-based lookups (summary / list filters).
    op.create_index(
        op.f('ix_inventory_audits_week_start'),
        'inventory_audits',
        ['week_start'],
        unique=False,
    )

    # 6. One audit per outlet per week. Partial so soft-deleted rows don't block
    #    re-generation.
    op.create_index(
        'uq_audit_outlet_week',
        'inventory_audits',
        ['outlet_id', 'week_start'],
        unique=True,
        postgresql_where=sa.text('deleted_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_audit_outlet_week', table_name='inventory_audits')
    op.drop_index(op.f('ix_inventory_audits_week_start'), table_name='inventory_audits')
    op.drop_column('inventory_audits', 'week_start')
