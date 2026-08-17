"""add crop translations table

Revision ID: a1b2c3d4e5f6
Revises: 737fdf61e95b
Create Date: 2026-08-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from config import get_settings

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '737fdf61e95b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = get_settings().name


def _base_columns():
    return [
        sa.Column("uid", sa.String, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "crop_translations",
        *_base_columns(),
        sa.Column("crop_uid", sa.String, sa.ForeignKey(f"{SCHEMA}.crops.uid" if SCHEMA else "crops.uid"), nullable=False),
        sa.Column("lang", sa.String(5), nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.UniqueConstraint("crop_uid", "lang", name="uq_crop_translations_crop_uid_lang"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_crop_translations_crop_uid_lang",
        "crop_translations",
        ["crop_uid", "lang"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_crop_translations_crop_uid_lang", table_name="crop_translations", schema=SCHEMA)
    op.drop_table("crop_translations", schema=SCHEMA)
