"""seed disease_kb from cow_disease_kb.csv

Revision ID: 0007_seed_disease_kb
Revises: 0006_create_disease_reports_v1
Create Date: 2026-10-05 00:00:00.000000

Loads the 31 disease rows of cow_disease_kb.csv (kept next to this file in
migrations/data/) as version 1, lang_code 'en'.

kb_uid is a UUIDv5 of the disease_id and version, so the same KB entry gets the
same kb_uid in every environment and reports stay comparable across them.
"""
import csv
import uuid
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007_seed_disease_kb'
down_revision: Union[str, None] = '0006_create_disease_reports_v1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CSV_PATH = Path(__file__).parents[1] / 'data' / 'cow_disease_kb.csv'
KB_UID_NAMESPACE = uuid.UUID('6f1c2b1e-4a57-4d0e-9a51-6b0d2f0c7a11')
VERSION = 1


def kb_uid_for(disease_id: str, version: int) -> str:
    return str(uuid.uuid5(KB_UID_NAMESPACE, f'disease_kb:{disease_id}:v{version}'))


def _rows() -> list[dict]:
    with open(CSV_PATH, encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    out = []
    for row in rows:
        record = {key.lower(): (value or '').strip() or None for key, value in row.items()}
        record.update(
            uid=f"disease_kb_{uuid.uuid4()}",
            kb_uid=kb_uid_for(record['disease_id'], VERSION),
            lang_code='en',
            version=VERSION,
            is_active=True,
        )
        out.append(record)
    return out


_TYPES = {
    'version': sa.Integer(),
    'is_active': sa.Boolean(),
}


def upgrade() -> None:
    rows = _rows()
    disease_kb = sa.table('disease_kb', *[sa.column(name, _TYPES.get(name, sa.Text())) for name in rows[0]])
    op.bulk_insert(disease_kb, rows)


def downgrade() -> None:
    kb_uids = [row['kb_uid'] for row in _rows()]
    disease_kb = sa.table('disease_kb', sa.column('kb_uid'), sa.column('lang_code'))
    op.execute(
        disease_kb.delete().where(disease_kb.c.kb_uid.in_(kb_uids), disease_kb.c.lang_code == 'en')
    )
