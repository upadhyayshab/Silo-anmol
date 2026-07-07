"""Add selling_price to products

Selling price is the price we actually sell at: 0 < selling_price <= cost_price
(cost_price IS the MRP in this schema). Display-only on the product; the order
drawer pre-fills the line discount as (MRP - selling_price). Existing rows are
backfilled to cost_price (i.e. no markdown) so the column can be made NOT NULL.

Revision ID: 20260704_01_selling_price
Revises: 20260703_01_lead_segments
Create Date: 2026-07-04 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20260704_01_selling_price"
down_revision = "20260703_01_lead_segments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("selling_price", sa.Numeric(10, 2), nullable=True))
    # Backfill = MRP (no markdown) so the NOT NULL constraint holds for existing rows.
    op.execute("UPDATE products SET selling_price = cost_price WHERE selling_price IS NULL")
    op.alter_column("products", "selling_price", nullable=False)


def downgrade() -> None:
    op.drop_column("products", "selling_price")
