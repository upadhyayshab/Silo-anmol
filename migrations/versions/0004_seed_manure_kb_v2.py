"""seed manure_kb from master_table_v2.csv

Revision ID: 0004_seed_manure_kb_v2
Revises: 0003_create_manure_reports_v1
Create Date: 2026-10-01 00:00:00.000000

Loads the 27 sub-code rows of master_table_v2.csv (kept next to this file in
migrations/data/, since the image ships migrations/ but not the repo root) as
version 1, lang_code 'en'.

kb_uid is a UUIDv5 of the sub-code and version, so the same KB entry gets the
same kb_uid in every environment and reports stay comparable across them.
"""
import csv
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004_seed_manure_kb_v2'
down_revision: Union[str, None] = '0003_create_manure_reports_v1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CSV_PATH = Path(__file__).parents[1] / 'data' / 'master_table_v2.csv'
KB_UID_NAMESPACE = uuid.UUID('6f1c2b1e-4a57-4d0e-9a51-6b0d2f0c7a11')
VERSION = 1


def kb_uid_for(sub_code: str, version: int) -> str:
    return str(uuid.uuid5(KB_UID_NAMESPACE, f'manure_kb:{sub_code}:v{version}'))


def _rows() -> list[dict]:
    with open(CSV_PATH, encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    out = []
    for row in rows:
        record = {key.lower(): (value or '').strip() or None for key, value in row.items()}
        record['score'] = int(record['score'])
        record['min_confidence_to_report'] = Decimal(record['min_confidence_to_report'])
        record.update(
            uid=f'manure_kb_{uuid.uuid4()}',
            kb_uid=kb_uid_for(record['sub_code'], VERSION),
            lang_code='en',
            version=VERSION,
            is_active=True,
        )
        out.append(record)
    return out


_TYPES = {
    'score': sa.SmallInteger(),
    'version': sa.Integer(),
    'is_active': sa.Boolean(),
    'min_confidence_to_report': sa.Numeric(3, 2),
}


def upgrade() -> None:
    rows = _rows()
    # Typed so `alembic upgrade --sql` can render the values as literals too.
    manure_kb = sa.table('manure_kb', *[sa.column(name, _TYPES.get(name, sa.Text())) for name in rows[0]])
    op.bulk_insert(manure_kb, rows)


def downgrade() -> None:
    kb_uids = [row['kb_uid'] for row in _rows()]
    manure_kb = sa.table('manure_kb', sa.column('kb_uid'), sa.column('lang_code'))
    op.execute(
        manure_kb.delete().where(manure_kb.c.kb_uid.in_(kb_uids), manure_kb.c.lang_code == 'en')
    )
