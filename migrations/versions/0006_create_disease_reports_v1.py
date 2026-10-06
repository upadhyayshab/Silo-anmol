"""create disease_reports_v1

Revision ID: 0006_create_disease_reports_v1
Revises: 0005_create_disease_kb
Create Date: 2026-10-05 00:00:00.000000

One row per cattle disease screening scan: who/what was scanned, whether it was
a cattle image, the detected view, the structured JSON findings array, summary counts,
and audit/latency metrics.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0006_create_disease_reports_v1'
down_revision: Union[str, None] = '0005_create_disease_kb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'disease_reports_v1',
        sa.Column('uid', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('cow_id', sa.String(), nullable=True),
        sa.Column('lang_code', sa.String(length=8), server_default='en', nullable=False),
        sa.Column('img_url', sa.String(), nullable=True),
        sa.Column('is_cattle_image', sa.Boolean(), nullable=False),
        sa.Column('image_view', sa.String(length=64), nullable=True),
        sa.Column('findings', sa.JSON(), nullable=True),
        sa.Column('detected_count', sa.SmallInteger(), server_default='0', nullable=True),
        sa.Column('suspected_count', sa.SmallInteger(), server_default='0', nullable=True),
        sa.Column('primary_diagnosis', sa.String(), nullable=True),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('uid'),
    )
    op.create_index('ix_disease_reports_v1_user_id', 'disease_reports_v1', ['user_id'])
    op.create_index('ix_disease_reports_v1_cow_id', 'disease_reports_v1', ['cow_id'])
    op.create_index('ix_disease_reports_v1_created_at', 'disease_reports_v1', ['created_at'])


def downgrade() -> None:
    op.drop_table('disease_reports_v1')
