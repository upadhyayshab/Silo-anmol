"""add deleted_at to user_crop_reports, crop_reports and event_outbox

Every model inherits deleted_at from SharedBackend's BaseSchema, so the app
selects and inserts it on these three tables, but f1a2b3c4d5e6 created them
without it. On a database built from the migrations alone, saving a crop
guide run, reading runs and reports, seeding or reading crop_reports, and
parking an event in event_outbox all failed with "column deleted_at does
not exist". Dev already has the column, hence IF NOT EXISTS. Additive only.

Revision ID: j1a2b3c4d5e6
Revises: i1a2b3c4d5e6
Create Date: 2026-10-19 00:00:00.000000
"""
from alembic import op

from config import get_settings

revision = "j1a2b3c4d5e6"
down_revision = "i1a2b3c4d5e6"
branch_labels = None
depends_on = None

SCHEMA = get_settings().name
TABLES = ("user_crop_reports", "crop_reports", "event_outbox")


def upgrade():
    for table in TABLES:
        op.execute(f'ALTER TABLE "{SCHEMA}".{table} ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE')


def downgrade():
    # Deliberately keeps the column: the models need it, and on dev it existed
    # before this revision, so dropping it would break the running app.
    pass
