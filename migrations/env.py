import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parents[1] / "app"))
sys.path.append(str(Path(__file__).parents[1] / "SharedBackend" / "src"))

from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool, text

from alembic import context

from managers import *  # noqa
from SharedBackend.managers import BaseSchema
from config import get_settings

config = context.config
settings = get_settings()

if config.config_file_name is not None: fileConfig(config.config_file_name)

# for 'autogenerate' support
target_metadata = BaseSchema.metadata

config.set_main_option("sqlalchemy.url", settings.engine_str.replace("+aiosqlite", "").replace("+asyncpg", ""))

# This service's schema. It must be explicit: the shared dev DB login is named
# after another service's schema, so Postgres' default search_path ("$user")
# would otherwise put these tables — and alembic_version — in that schema.
SCHEMA = settings.name if settings.supports_schema else None


def _include_object(obj, name, type_, reflected, compare_to):
    # With search_path pinned to SCHEMA, autogenerate reflects alembic's own
    # version table as an unknown table and proposes dropping it.
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=SCHEMA,
    )

    with context.begin_transaction():
        if SCHEMA:
            context.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
            context.execute(f'SET search_path TO "{SCHEMA}"')
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        if SCHEMA:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
            connection.execute(text(f'SET search_path TO "{SCHEMA}"'))
            connection.commit()
        context.configure(
            connection=connection, target_metadata=target_metadata, version_table_schema=SCHEMA,
            include_object=_include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
