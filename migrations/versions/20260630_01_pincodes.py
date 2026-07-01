"""Create the `pincodes` reference table (telecaller geo auto-fill).

Dev/CRM only — do not ride this onto the prod chain (prod excludes CRM).

Revision ID: 20260630_01_pincodes
Revises: 20260629_02_user_role_to_varchar
"""
from alembic import op
import sqlalchemy as sa

revision = "20260630_01_pincodes"
down_revision = "20260629_02_user_role_to_varchar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pincodes",
        sa.Column("uid", sa.String(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pincode", sa.String(length=10), nullable=False),
        sa.Column("state", sa.String(length=100), nullable=False),
        sa.Column("district", sa.String(length=100), nullable=True),
        sa.Column("taluk", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("uid"),
    )
    op.create_index("ix_pincodes_pincode", "pincodes", ["pincode"])


def downgrade() -> None:
    op.drop_index("ix_pincodes_pincode", table_name="pincodes")
    op.drop_table("pincodes")
