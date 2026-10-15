"""Backfill recommendation_history.{farm_id,farm_name,livestock,current_crop_code,sowing_date}

f1a2b3c4d5e6 added these columns but alembic_version records it as applied
on this dev schema while the columns are actually absent -- the other three
tables that migration created (event_outbox, crop_reports, user_crop_reports)
exist fine, only these five add_column calls on the pre-existing
recommendation_history table never landed. Cause undetermined (not worth
re-litigating); this just gets the schema to what f1a2b3c4d5e6 already
declared, using IF NOT EXISTS so it's safe to run anywhere regardless of
whether a given environment actually has the gap.

Revision ID: g1a2b3c4d5e6
Revises: f1a2b3c4d5e6
Create Date: 2026-10-15 00:00:00.000000
"""
from alembic import op

from config import get_settings

revision = "g1a2b3c4d5e6"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None

SCHEMA = get_settings().name


def upgrade():
    op.execute(f'ALTER TABLE "{SCHEMA}".recommendation_history ADD COLUMN IF NOT EXISTS farm_id VARCHAR(255)')
    op.execute(f'ALTER TABLE "{SCHEMA}".recommendation_history ADD COLUMN IF NOT EXISTS farm_name VARCHAR(255)')
    op.execute(f'ALTER TABLE "{SCHEMA}".recommendation_history ADD COLUMN IF NOT EXISTS livestock VARCHAR(255)')
    op.execute(f'ALTER TABLE "{SCHEMA}".recommendation_history ADD COLUMN IF NOT EXISTS current_crop_code VARCHAR(255)')
    op.execute(f'ALTER TABLE "{SCHEMA}".recommendation_history ADD COLUMN IF NOT EXISTS sowing_date DATE')


def downgrade():
    op.execute(f'ALTER TABLE "{SCHEMA}".recommendation_history DROP COLUMN IF EXISTS sowing_date')
    op.execute(f'ALTER TABLE "{SCHEMA}".recommendation_history DROP COLUMN IF EXISTS current_crop_code')
    op.execute(f'ALTER TABLE "{SCHEMA}".recommendation_history DROP COLUMN IF EXISTS livestock')
    op.execute(f'ALTER TABLE "{SCHEMA}".recommendation_history DROP COLUMN IF EXISTS farm_name')
    op.execute(f'ALTER TABLE "{SCHEMA}".recommendation_history DROP COLUMN IF EXISTS farm_id')
