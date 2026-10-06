"""create manure_kb

Revision ID: 0002_create_manure_kb
Revises: 0001_shared_backend_tables
Create Date: 2026-10-01 00:00:00.000000

The manure knowledge base: the 40 master_table_v2.csv columns (snake_cased)
plus identity/versioning (kb_uid, lang_code, version, is_active) and two
columns the KB owners fill in later (call_vet_if, pr). Descriptive columns are
nullable so score-level free-tier rows (sub_code like '4_FREE') fit too.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002_create_manure_kb'
down_revision: Union[str, None] = '0001_shared_backend_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TEXT_COLUMNS = (
    'geometry_3d_form', 'top_down_2d_contour_signature', 'central_dimple_morphology',
    'perimeter_edge_slope', 'texture_and_moisture_class', 'radial_bleed_ring_extent',
    'substrate_floor_interaction', 'surface_optical_reflectance', 'biological_sheen_vs_flash_glare_rule',
    'hex_range', 'lab_centroids', 'dietary_color_confounder_disambiguation',
    'fiber_mastication_and_particle_length', 'gas_bubbles_foam_morphology',
    'mucus_fibrin_and_blood_morphology', 'freshness_vs_sun_crust_gate', 'mandatory_visual_inclusions',
    'mandatory_visual_exclusions', 'primary_structural_discriminator', 'visual_lookalike_discriminator',
    'strict_negative_exclusion_logic', 'camera_bias_and_confounder_gates', 'animal_context_gate',
    'abstain_escalate_if',
    'diagnosed_clinical_condition', 'root_cause_etiology', 'milk_withdrawal_risk', 'evidence_basis',
    'on_farm_confirmatory_check', 'actionable_advice',
    'call_vet_if',
)


def upgrade() -> None:
    op.create_table(
        'manure_kb',
        sa.Column('uid', sa.String(), nullable=False),
        sa.Column('kb_uid', sa.String(length=36), nullable=False),
        sa.Column('lang_code', sa.String(length=8), server_default='en', nullable=False),
        sa.Column('sub_code', sa.String(length=32), nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('score', sa.SmallInteger(), nullable=False),
        sa.Column('consistency_class', sa.String(length=32), nullable=True),
        sa.Column('sub_type_name', sa.String(), nullable=True),
        sa.Column('colour_group', sa.String(length=32), nullable=True),
        sa.Column('severity', sa.String(length=20), nullable=True),
        sa.Column('action_type_enum', sa.String(length=64), nullable=True),
        sa.Column('vet_gate', sa.String(length=10), nullable=True),
        sa.Column('re_scan_window', sa.String(), nullable=True),
        sa.Column('min_confidence_to_report', sa.Numeric(3, 2), nullable=True),
        *[sa.Column(name, sa.Text(), nullable=True) for name in TEXT_COLUMNS],
        sa.Column('pr', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('uid'),
        sa.UniqueConstraint('kb_uid', 'lang_code', name='uq_manure_kb_kb_uid_lang'),
        sa.CheckConstraint('score BETWEEN 1 AND 5', name='ck_manure_kb_score'),
        sa.CheckConstraint(
            "severity IN ('NORMAL_BASELINE', 'MONITOR', 'WARNING', 'URGENT', 'EMERGENCY')",
            name='ck_manure_kb_severity',
        ),
        sa.CheckConstraint("vet_gate IN ('NO', 'ADVISED', 'MANDATORY')", name='ck_manure_kb_vet_gate'),
        sa.CheckConstraint('min_confidence_to_report BETWEEN 0 AND 1', name='ck_manure_kb_min_confidence'),
    )
    op.create_index('ix_manure_kb_kb_uid', 'manure_kb', ['kb_uid'])
    op.create_index('ix_manure_kb_lang_code', 'manure_kb', ['lang_code'])
    op.create_index('ix_manure_kb_score', 'manure_kb', ['score'])
    op.create_index('ix_manure_kb_severity', 'manure_kb', ['severity'])
    # At most one live version of a sub-code per language.
    op.create_index(
        'uq_manure_kb_active_sub_code_lang', 'manure_kb', ['sub_code', 'lang_code'], unique=True,
        postgresql_where=sa.text('is_active AND deleted_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_table('manure_kb')
