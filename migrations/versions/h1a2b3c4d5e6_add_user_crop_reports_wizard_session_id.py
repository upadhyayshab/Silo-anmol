"""add user_crop_reports.wizard_session_id (submit idempotency)

A retried or double-tapped wizard submit used to charge the subscription
twice and create two farms in Agro. The submit handler now stores the
client's wizard_session_id with the run and replays that run when the same
id arrives again. Additive only; the unique index is partial (non-null ids),
so runs without a session id are unaffected, and it makes a concurrent
duplicate fail its insert instead of storing a second run.

Revision ID: h1a2b3c4d5e6
Revises: g1a2b3c4d5e6
Create Date: 2026-10-17 00:00:00.000000
"""
from alembic import op

from config import get_settings

revision = "h1a2b3c4d5e6"
down_revision = "g1a2b3c4d5e6"
branch_labels = None
depends_on = None

SCHEMA = get_settings().name


def upgrade():
    op.execute(f'ALTER TABLE "{SCHEMA}".user_crop_reports ADD COLUMN IF NOT EXISTS wizard_session_id VARCHAR(128)')
    op.execute(
        f'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_crop_reports_user_session '
        f'ON "{SCHEMA}".user_crop_reports (user_id, wizard_session_id) WHERE wizard_session_id IS NOT NULL'
    )


def downgrade():
    op.execute(f'DROP INDEX IF EXISTS "{SCHEMA}".uq_user_crop_reports_user_session')
    op.execute(f'ALTER TABLE "{SCHEMA}".user_crop_reports DROP COLUMN IF EXISTS wizard_session_id')
