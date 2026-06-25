"""leads: partial unique index on mobile (dedup race safety net)

Closes the check-then-insert dedup race: concurrent creates of the same lead
(FB webhook + backfill firing ms apart) could both pass find_duplicate and
insert. This index makes Postgres reject the loser; create_lead catches the
IntegrityError and merges instead. Excludes soft-deleted rows and blank mobiles.

NOTE: existing duplicates must be cleaned (soft-deleted) BEFORE this runs, or
index creation fails. See scripts/dedupe_existing_leads.py.

Revision ID: 20260624_01_leads_mobile_unique
Revises: 20260623_01_rbac_roles_scope
Create Date: 2026-06-24 00:00:00.000000
"""

from alembic import op


revision = "20260624_01_leads_mobile_unique"
down_revision = "20260623_01_rbac_roles_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: dev/prod migration chains have diverged (CRM track is
    # dev-only) and this index may be created out-of-band on dev, so the
    # migration must be a safe no-op if it already exists.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_mobile_active "
        "ON leads (mobile) WHERE deleted_at IS NULL AND mobile <> ''"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_leads_mobile_active")
