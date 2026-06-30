"""Convert `users.role` from a Postgres ENUM to varchar(64).

DB-backed RBAC: a user's role is now a name referencing the `roles` table (system or
admin-created), so the column must hold arbitrary role names — not a fixed enum. We cast
the enum column to text in place; existing values (the 25 known role names) survive
untouched. No hard FK to roles.name (validated at the app layer; roles are soft-deletable).

The old enum TYPE is deliberately left in place — sibling services may share this DB and
reference their own users.role enum (per the agency/cluster migration notes); dropping it
could break them, and an orphaned type is harmless.

Not auto-reverted: downgrade is a no-op. varchar is a safe superset of the enum (no data
loss), and silently re-imposing a hard enum on a shared/prod DB is the risky direction we
don't want a downgrade to take. To revert manually, ALTER the column back to the enum type.

ponytail: re-parent down_revision onto the prod chain before this rides to prod
(prod excludes CRM/FB — known divergence).

Revision ID: 20260629_02_user_role_to_varchar
Revises: 20260629_01_rbac_roles_tables
"""
from alembic import op
import sqlalchemy as sa

revision = "20260629_02_user_role_to_varchar"
down_revision = "20260629_01_rbac_roles_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # sqlite stores Enum as VARCHAR already — nothing to convert
    # Idempotency: information_schema reports an enum column as 'USER-DEFINED' and a
    # converted one as 'character varying'. (Don't trust the SQLAlchemy-reflected type —
    # str(ENUM) compiles to 'VARCHAR(n)', which would falsely look already-converted.)
    data_type = bind.execute(sa.text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name='users' AND column_name='role' "
        "AND table_schema=current_schema()")).scalar()
    if data_type != "USER-DEFINED":
        return  # already varchar/text
    op.alter_column(
        "users", "role",
        type_=sa.String(64),
        existing_nullable=False,
        postgresql_using="role::text",
    )


def downgrade() -> None:
    # Intentional no-op — see module docstring. varchar holds the same role names, so
    # the prior (enum-column) code keeps working; reverting to a hard enum is manual.
    pass
