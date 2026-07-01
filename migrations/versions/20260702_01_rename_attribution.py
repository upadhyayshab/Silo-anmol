"""Rename `lsq_order_ad` -> `order_attribution`.

The table is no longer LSQ-specific: it holds order attribution for LSQ, the
in-house CRM, and (soon) Medusa D2C. Rename the misnomer while there are only
a couple of consumers.

PROD-BOUND (unlike the CRM-only migrations it sits behind): order_attribution
exists in prod. Chained on the current dev head (`20260630_02_lead_taluk`)
per house convention; re-parent `down_revision` onto the prod chain before this
rides to prod (prod excludes CRM/FB — known divergence).

Revision ID: 20260702_01_rename_attribution
Revises: 20260630_02_lead_taluk
"""
from alembic import op
import sqlalchemy as sa

revision = "20260702_01_rename_attribution"
down_revision = "20260630_02_lead_taluk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: on a shared dev DB create_all may already have built the table
    # under the new name; on a fresh DB neither exists. Only rename a real, un-renamed table.
    # ponytail: keeps the old lsq_order_ad_* index/constraint names; harmless, rename them if it ever bugs anyone.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "lsq_order_ad" in tables and "order_attribution" not in tables:
        op.rename_table("lsq_order_ad", "order_attribution")


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "order_attribution" in tables and "lsq_order_ad" not in tables:
        op.rename_table("order_attribution", "lsq_order_ad")
