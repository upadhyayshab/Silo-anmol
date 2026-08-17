import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parents[1] / "app"))
sys.path.append(str(Path(__file__).parents[1] / "SharedBackend" / "src"))

from logging.config import fileConfig

import sqlalchemy as sa
from sqlalchemy import engine_from_config, text
from sqlalchemy import pool

from alembic import context

from managers import *  # noqa
from SharedBackend.managers import BaseSchema
from config import get_settings

config = context.config
settings = get_settings()

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# for 'autogenerate' support
target_metadata = BaseSchema.metadata

# Build synchronous psycopg2 URL (strip +asyncpg / +aiosqlite for Alembic)
_sync_url = settings.engine_str.replace("+asyncpg", "").replace("+aiosqlite", "")
# configparser (used by Alembic) treats % as an interpolation prefix, so any
# percent-encoded chars in the URL (e.g. %40 for @, %23 for #) must be escaped
# as %% before being stored. configparser un-escapes %% -> % on read-back.
config.set_main_option("sqlalchemy.url", _sync_url.replace("%", "%%"))

SCHEMA = settings.name  # e.g. "kissan_cr_v2_dev"

# ---------------------------------------------------------------------------
# Migration history — append-only audit log (one row per upgrade/downgrade)
# ---------------------------------------------------------------------------
# Standard alembic_version only stores the CURRENT head (one row, updated
# in-place). migration_history supplements it with a full date-stamped log.

_CREATE_HISTORY = f"""
CREATE TABLE IF NOT EXISTS "{SCHEMA}"."migration_history" (
    id          SERIAL       PRIMARY KEY,
    revision    VARCHAR(64)  NOT NULL,
    direction   VARCHAR(16)  NOT NULL CHECK (direction IN ('upgrade', 'downgrade')),
    applied_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    description TEXT
);
"""

_INSERT_HISTORY = f"""
INSERT INTO "{SCHEMA}"."migration_history" (revision, direction)
VALUES (:revision, :direction);
"""


def _bootstrap(connection):
    """Bootstrap migration_history table if the app schema already exists.

    On the very first run the schema doesn't exist yet — it gets created by
    the first migration (upgrade). We check for the schema first and skip
    silently if it isn't there yet; the first migration will create both the
    schema and the history table via a dedicated step.
    """
    result = connection.execute(
        text(
            "SELECT 1 FROM information_schema.schemata "
            "WHERE schema_name = :s"
        ),
        {"s": SCHEMA},
    ).fetchone()
    if result:
        connection.execute(text(_CREATE_HISTORY))
        connection.commit()


def _log_migration(connection, revision, direction):
    connection.execute(
        text(_INSERT_HISTORY),
        {"revision": revision, "direction": direction},
    )
    connection.commit()


# ---------------------------------------------------------------------------
# Alembic runner functions
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """Offline mode — emit SQL to stdout without a live DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="alembic_version",
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Online mode — connect to DB, bootstrap audit table, run migrations."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Bootstrap: schema + history table must exist before Alembic runs
        _bootstrap(connection)

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # alembic_version lives inside the app schema (not public)
            version_table="alembic_version",
            version_table_schema=SCHEMA,
            # Hook: fires after each revision is successfully applied/reversed
            on_version_apply=lambda ctx, step, heads, run_args: (
                _log_migration(
                    connection=connection,
                    revision=step.up_revision_id or step.down_revision_id,
                    direction="upgrade" if step.is_upgrade else "downgrade",
                )
            ),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
