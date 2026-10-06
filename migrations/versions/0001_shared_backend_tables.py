"""create SharedBackend base tables

Revision ID: 0001_shared_backend_tables
Revises:
Create Date: 2026-10-01 00:00:00.000000

The tables SharedBackend's middlewares and the template example router need
(entities, api_keys, scopes, api_key_scopes, aes256_encryption_keys,
examples). They used to be created by create_all at app startup; alembic now
owns every table in this schema.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001_shared_backend_tables'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column('uid', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        'aes256_encryption_keys',
        sa.Column('key', sa.String(), nullable=True),
        *_base_columns(),
        sa.PrimaryKeyConstraint('uid'),
    )
    op.create_table(
        'api_keys',
        sa.Column('key_hash', sa.String(), nullable=True),
        sa.Column('type', sa.String(), nullable=False),
        *_base_columns(),
        sa.PrimaryKeyConstraint('uid'),
    )
    op.create_table(
        'entities',
        sa.Column('upstreamId', sa.String(), nullable=False),
        sa.Column('tablename', sa.String(), nullable=False),
        sa.Column('downstreamId', sa.String(), nullable=False),
        *_base_columns(),
        sa.PrimaryKeyConstraint('uid'),
        sa.UniqueConstraint('tablename', 'downstreamId'),
    )
    op.create_table(
        'examples',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=False),
        sa.Column('migration_test', sa.String(), server_default='migration-test', nullable=False),
        *_base_columns(),
        sa.PrimaryKeyConstraint('uid'),
    )
    op.create_table(
        'scopes',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('type', sa.String(), nullable=False),
        *_base_columns(),
        sa.PrimaryKeyConstraint('name', 'uid'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'api_key_scopes',
        sa.Column('api_key_uid', sa.String(), nullable=False),
        sa.Column('scope_name', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['api_key_uid'], ['api_keys.uid']),
        sa.ForeignKeyConstraint(['scope_name'], ['scopes.name']),
        sa.PrimaryKeyConstraint('api_key_uid', 'scope_name'),
    )


def downgrade() -> None:
    op.drop_table('api_key_scopes')
    op.drop_table('scopes')
    op.drop_table('examples')
    op.drop_table('entities')
    op.drop_table('api_keys')
    op.drop_table('aes256_encryption_keys')
