"""create manure_reports_v1

Revision ID: 0003_create_manure_reports_v1
Revises: 0002_create_manure_kb
Create Date: 2026-10-01 00:00:00.000000

One row per manure scan: who/what was scanned, the score and sub-score, the
manure_kb entry the report body was built from (kb_reference_id = its
kb_uid), the Gemini visual-evidence tags, the stored body itself, and the
model/latency audit fields.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003_create_manure_reports_v1'
down_revision: Union[str, None] = '0002_create_manure_kb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'manure_reports_v1',
        sa.Column('uid', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('cow_id', sa.String(), nullable=True),
        sa.Column('subs_id', sa.String(), nullable=True),
        sa.Column('has_subs', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('ui_version', sa.String(length=8), nullable=False),
        sa.Column('lang_code', sa.String(length=8), server_default='en', nullable=False),
        sa.Column('img_url', sa.String(), nullable=True),
        sa.Column('manure_score', sa.SmallInteger(), nullable=False),
        sa.Column('manure_sub_score', sa.String(length=32), nullable=True),
        sa.Column('kb_reference_id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('severity', sa.String(length=20), nullable=True),
        sa.Column('priority', sa.String(length=20), nullable=True),
        sa.Column('call_vet', sa.Boolean(), nullable=False),
        sa.Column('vet_gate', sa.String(length=10), nullable=True),
        sa.Column('score_breakdown', sa.JSON(), nullable=True),
        sa.Column('visual_evidence_as_tags', sa.JSON(), nullable=True),
        sa.Column('data_meta_json', sa.JSON(), nullable=True),
        sa.Column('premium_meta_json', sa.JSON(), nullable=True),
        sa.Column('pr', sa.JSON(), nullable=True),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('uid'),
        sa.CheckConstraint('manure_score BETWEEN 1 AND 5', name='ck_manure_reports_v1_score'),
    )
    op.create_index('ix_manure_reports_v1_user_id', 'manure_reports_v1', ['user_id'])
    op.create_index('ix_manure_reports_v1_cow_id', 'manure_reports_v1', ['cow_id'])
    op.create_index('ix_manure_reports_v1_kb_reference_id', 'manure_reports_v1', ['kb_reference_id'])


def downgrade() -> None:
    op.drop_table('manure_reports_v1')
