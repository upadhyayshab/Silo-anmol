"""fb_field_mappings.label — ops-maintained English label per question

Adds a nullable display label so a question asked in a regional language
(e.g. Kannada) can be identified in English on the lead. `label IS NULL`
is the flag: the question is untranslated and surfaces to ops as "pending".
Display-only — routing still uses `target`/`target_kind`.

Revision ID: 20260624_02_fb_question_label
Revises: 20260624_01_leads_mobile_unique
Create Date: 2026-06-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260624_02_fb_question_label"
down_revision = "20260624_01_leads_mobile_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: on a shared dev DB the column may already exist (added out-of-band
    # when alembic's version pointer lagged the live schema). Safe to re-run.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = [c["name"] for c in insp.get_columns("fb_field_mappings")]
    if "label" not in cols:
        op.add_column(
            "fb_field_mappings",
            sa.Column("label", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("fb_field_mappings", "label")
