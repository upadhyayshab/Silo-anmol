"""seed multilingual disease_kb translations across 13 Indian languages

Revision ID: 0008_seed_disease_kb_multilingual
Revises: 0007_seed_disease_kb
Create Date: 2026-10-07 00:00:00.000000

Loads the 403 translated disease rows for 13 languages (kn, hi, ta, te, ml, mr,
gu, bn, or, pa, ur, ne, as) into disease_kb from cow_disease_kb_multilingual.json.
All translations for a disease share the same kb_uid as the English row.
"""
import json
import uuid
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0008_seed_disease_multilingual'
down_revision: Union[str, None] = '0007_seed_disease_kb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DATA_PATH = Path(__file__).parents[1] / 'data' / 'cow_disease_kb_multilingual.json'


def _rows() -> list[dict]:
    with open(DATA_PATH, encoding='utf-8') as f:
        all_rows = json.load(f)
    
    # Filter only non-English translations since English was already seeded in 0007
    non_en_rows = [r for r in all_rows if r.get('lang_code') != 'en']
    out = []
    for row in non_en_rows:
        record = {key.lower(): (value if value is not None else None) for key, value in row.items()}
        record.update(
            uid=f"disease_kb_{uuid.uuid4()}",
            version=1,
            is_active=True,
        )
        out.append(record)
    return out


_TYPES = {
    'version': sa.Integer(),
    'is_active': sa.Boolean(),
    'pr': sa.JSON(),
}


def upgrade() -> None:
    rows = _rows()
    if not rows:
        return
    disease_kb = sa.table('disease_kb', *[sa.column(name, _TYPES.get(name, sa.Text())) for name in rows[0]])
    op.bulk_insert(disease_kb, rows)


def downgrade() -> None:
    non_en_langs = ['kn', 'hi', 'ta', 'te', 'ml', 'mr', 'gu', 'bn', 'or', 'pa', 'ur', 'ne', 'as']
    disease_kb = sa.table('disease_kb', sa.column('lang_code'))
    op.execute(
        disease_kb.delete().where(disease_kb.c.lang_code.in_(non_en_langs))
    )
