"""add_pincode_zone_mapping

Revision ID: 737fdf61e95b
Revises: 8227319bdab2
Create Date: 2026-08-10 15:29:17.730115

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '737fdf61e95b'
down_revision: Union[str, None] = '8227319bdab2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


from config import get_settings

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
        "pincode_zone_mappings",
        *_base_columns(),
        sa.Column("pincode", sa.String(10), nullable=False, unique=True),
        sa.Column("zone", sa.Integer, nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("pincode_zone_mappings", schema=SCHEMA)
