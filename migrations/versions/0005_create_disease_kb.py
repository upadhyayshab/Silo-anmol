"""create disease_kb

Revision ID: 0005_create_disease_kb
Revises: 0004_seed_manure_kb_v2
Create Date: 2026-10-05 00:00:00.000000

The cattle disease knowledge base: the 21 cow_disease_kb.csv columns
plus identity/versioning (kb_uid, lang_code, version, is_active) and pr metadata.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0005_create_disease_kb'
down_revision: Union[str, None] = '0004_seed_manure_kb_v2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TEXT_COLUMNS = (
    'also_known_as',
    'required_image_view',
    'regions_to_inspect',
    'key_visual_signs',
    'supporting_visual_signs',
    'posture_gait_behaviour_cues',
    'look_alikes_and_how_to_differentiate',
    'signs_that_argue_against',
    'minimum_evidence_to_report',
    'confidence_rules',
    'image_quality_needed',
    'notes_for_model',
    'severity_and_urgency',
    'risk_context',
    'farmer_question',
    'confirmatory_action',
)


def upgrade() -> None:
    op.create_table(
        'disease_kb',
        sa.Column('uid', sa.String(), nullable=False),
        sa.Column('kb_uid', sa.String(length=36), nullable=False),
        sa.Column('lang_code', sa.String(length=8), server_default='en', nullable=False),
        sa.Column('disease_id', sa.String(length=32), nullable=False),
        sa.Column('disease_name', sa.String(), nullable=False),
        sa.Column('system', sa.String(length=128), nullable=True),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('detectable_from_side_image', sa.String(length=128), nullable=True),
        sa.Column('visual_detectability', sa.String(length=128), nullable=True),
        *[sa.Column(name, sa.Text(), nullable=True) for name in TEXT_COLUMNS],
        sa.Column('pr', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('uid'),
        sa.UniqueConstraint('kb_uid', 'lang_code', name='uq_disease_kb_kb_uid_lang'),
    )
    op.create_index('ix_disease_kb_kb_uid', 'disease_kb', ['kb_uid'])
    op.create_index('ix_disease_kb_lang_code', 'disease_kb', ['lang_code'])
    op.create_index('ix_disease_kb_disease_id', 'disease_kb', ['disease_id'])
    op.create_index('ix_disease_kb_system', 'disease_kb', ['system'])
    # At most one live version of a disease per language.
    op.create_index(
        'uq_disease_kb_active_disease_id_lang', 'disease_kb', ['disease_id', 'lang_code'], unique=True,
        postgresql_where=sa.text('is_active AND deleted_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_table('disease_kb')
