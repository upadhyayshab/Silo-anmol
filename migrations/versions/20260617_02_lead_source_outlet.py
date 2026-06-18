"""Add 'Outlet' value to the lead_source enum

"Outlet" is a new LeadSource used to tag leads first captured at an outlet
(sent to LeadSquared on order sync — see crm_utils.build_crm_payload). The same
LeadSource enum also backs the native-CRM `leads.source` column, a Postgres
enum. Postgres enums are closed sets, so the new value must be registered on the
`lead_source` type to keep the DB in sync with the Python enum and avoid a
failed insert/update should a native lead ever be given this source.

Revision ID: 20260617_02_lead_source_outlet
Revises: 20260617_01_cluster_hierarchy
Create Date: 2026-06-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20260617_02_lead_source_outlet"
down_revision = "20260617_01_cluster_hierarchy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Other dialects (e.g. sqlite in tests) store the enum as plain text.
        return
    # The `lead_source` type only exists where the native-CRM tables were
    # created. CRM is excluded from some environments (the migration graph
    # diverges), so skip cleanly when the type is absent rather than erroring.
    type_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'lead_source'")
    ).scalar()
    if not type_exists:
        return
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block, so step
    # outside Alembic's transaction. IF NOT EXISTS keeps it idempotent.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE lead_source ADD VALUE IF NOT EXISTS 'Outlet'")


def downgrade() -> None:
    # PostgreSQL has no native way to drop a single enum value; removing it would
    # require recreating the type and rewriting every dependent column. Leaving
    # the value in place is harmless, so the downgrade is intentionally a no-op.
    pass
