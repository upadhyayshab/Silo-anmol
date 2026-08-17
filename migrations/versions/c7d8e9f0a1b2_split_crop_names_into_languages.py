"""split crops.name into name_en + 13 language columns, drop crop_translations

Revision ID: c7d8e9f0a1b2
Revises: a1b2c3d4e5f6
Create Date: 2026-08-17

Collapses the per-row crop_translations table (crop_uid, lang, name) into wide
columns directly on crops: name_en (renamed from name) plus one nullable column
per remaining supported language. SUPPORTED_LANGUAGES (app/utils/languages.py)
uses 'hn' for Hindi going forward, but crop_translations stored Hindi rows under
'hi' -- that's the one code that gets remapped during backfill; every other
language matches its column suffix directly (name_kn <- lang='kn', etc.).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from config import get_settings

# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = get_settings().name

# new crops column -> crop_translations.lang value it is backfilled from.
LANG_BACKFILL = {
    "name_hn": "hi", "name_kn": "kn", "name_ta": "ta", "name_te": "te",
    "name_ml": "ml", "name_mr": "mr", "name_gu": "gu", "name_bn": "bn",
    "name_pa": "pa", "name_ur": "ur", "name_ne": "ne", "name_or": "or",
    "name_as": "as",
}


def upgrade() -> None:
    op.alter_column(
        "crops", "name",
        new_column_name="name_en",
        existing_type=sa.String,
        existing_nullable=False,
        schema=SCHEMA,
    )

    for col_name in LANG_BACKFILL:
        op.add_column("crops", sa.Column(col_name, sa.String, nullable=True), schema=SCHEMA)

    for col_name, lang in LANG_BACKFILL.items():
        op.execute(f"""
            UPDATE "{SCHEMA}".crops AS c
            SET {col_name} = ct.name
            FROM "{SCHEMA}".crop_translations AS ct
            WHERE ct.crop_uid = c.uid AND ct.lang = '{lang}'
        """)

    op.drop_index("ix_crop_translations_crop_uid_lang", table_name="crop_translations", schema=SCHEMA)
    op.drop_table("crop_translations", schema=SCHEMA)


def downgrade() -> None:
    op.create_table(
        "crop_translations",
        sa.Column("uid", sa.String, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("crop_uid", sa.String, sa.ForeignKey(f"{SCHEMA}.crops.uid"), nullable=False),
        sa.Column("lang", sa.String(5), nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.UniqueConstraint("crop_uid", "lang", name="uq_crop_translations_crop_uid_lang"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_crop_translations_crop_uid_lang", "crop_translations", ["crop_uid", "lang"], schema=SCHEMA,
    )

    reverse_map = {"name_en": "en", **LANG_BACKFILL}
    for col_name, lang in reverse_map.items():
        op.execute(f"""
            INSERT INTO "{SCHEMA}".crop_translations (uid, crop_uid, lang, name)
            SELECT 'ctrans-' || c.uid || '-' || '{lang}', c.uid, '{lang}', c.{col_name}
            FROM "{SCHEMA}".crops AS c
            WHERE c.{col_name} IS NOT NULL
        """)

    for col_name in LANG_BACKFILL:
        op.drop_column("crops", col_name, schema=SCHEMA)

    op.alter_column(
        "crops", "name_en",
        new_column_name="name",
        existing_type=sa.String,
        existing_nullable=False,
        schema=SCHEMA,
    )
