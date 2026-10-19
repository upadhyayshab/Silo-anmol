"""Every column the models read or write must be created by a migration.

The shared BaseSchema gives every table a deleted_at column, but the migration
that created user_crop_reports, crop_reports and event_outbox never added it.
Dev had the column from elsewhere, so nothing failed there; a database built
from the migrations alone (prod) rejected every save and read of a crop guide
run, every crop info page and every outbox write.

This replays all migrations in Alembic's offline mode (SQL is generated, no
database is touched) and compares the tables and columns they create with the
models in app/managers.
"""
import io
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from managers import cropManagers, cropReportManagers
from SharedBackend.managers import BaseSchema

REPO = Path(__file__).resolve().parents[1]
NAME = r'(?:(?:"[^"]+"|\w+)\.)?"?(\w+)"?'  # optionally schema-qualified (names may hold "-"), optionally quoted


class _NoRows:
    """What a data migration's SELECT returns offline: nothing to skip."""

    def fetchall(self):
        return []

    def scalar(self):
        return None

    def first(self):
        return None


class _OfflineBind:
    """Offline binds emit SQL and return None; data migrations then call
    .fetchall() on it. Keep the emitted SQL, return an empty result."""

    def __init__(self, bind):
        self._bind = bind

    def execute(self, *args, **kwargs):
        self._bind.execute(*args, **kwargs)
        return _NoRows()

    def __getattr__(self, name):
        return getattr(self._bind, name)


def _migration_sql(monkeypatch) -> str:
    real_get_bind = Operations.get_bind
    monkeypatch.setattr(Operations, "get_bind", lambda self: _OfflineBind(real_get_bind(self)))
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "migrations"))
    script = ScriptDirectory.from_config(config)
    out = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": out, "literal_binds": True},
    )
    with Operations.context(context):
        for revision in reversed(list(script.walk_revisions("base", "heads"))):
            revision.module.upgrade()
    return out.getvalue()


def _schema_from_sql(sql: str) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for statement in sql.split(";"):
        s = " ".join(statement.split())
        if m := re.match(rf"CREATE TABLE (?:IF NOT EXISTS )?{NAME} \((.*)\)$", s, re.I):
            columns = set()
            for part in re.split(r",(?![^()]*\))", m.group(2)):
                word = part.strip().split(" ")[0].strip('"')
                if word and word.upper() not in {"PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "CHECK"}:
                    columns.add(word)
            tables[m.group(1)] = columns
        elif m := re.match(rf"DROP TABLE (?:IF EXISTS )?{NAME}", s, re.I):
            tables.pop(m.group(1), None)
        elif m := re.match(rf"ALTER TABLE {NAME} ADD COLUMN (?:IF NOT EXISTS )?\"?(\w+)\"?", s, re.I):
            tables.setdefault(m.group(1), set()).add(m.group(2))
        elif m := re.match(rf"ALTER TABLE {NAME} DROP COLUMN (?:IF EXISTS )?\"?(\w+)\"?", s, re.I):
            tables.get(m.group(1), set()).discard(m.group(2))
        elif m := re.match(rf"ALTER TABLE {NAME} RENAME (?:COLUMN )?\"?(\w+)\"? TO \"?(\w+)\"?", s, re.I):
            columns = tables.get(m.group(1), set())
            columns.discard(m.group(2))
            columns.add(m.group(3))
    return tables


def _model_tables() -> dict[str, set[str]]:
    schemas = [
        obj for module in (cropManagers, cropReportManagers) for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, BaseSchema) and obj is not BaseSchema
        and getattr(obj, "__tablename__", None)
    ]
    return {s.__tablename__: {c.name for c in s.__table__.columns} for s in schemas}


@pytest.fixture(scope="module")
def migrated(request):
    monkeypatch = pytest.MonkeyPatch()
    request.addfinalizer(monkeypatch.undo)
    return _schema_from_sql(_migration_sql(monkeypatch))


def test_migrations_create_every_model_table(migrated):
    missing = sorted(set(_model_tables()) - set(migrated))
    assert not missing, f"tables used by the app but never created by a migration: {missing}"


@pytest.mark.parametrize("table", sorted(_model_tables()))
def test_migrations_create_every_model_column(migrated, table):
    missing = sorted(_model_tables()[table] - migrated.get(table, set()))
    assert not missing, f"{table}: columns the app reads/writes but no migration creates: {missing}"


def test_parser_reads_the_migration_sql(migrated):
    """Guards the guard: the parser must see the known shape of a table."""
    assert {"uid", "user_id", "recommendation_id", "report_data", "wizard_session_id"} <= migrated["user_crop_reports"]
    assert "name_en" in migrated["crops"]  # renamed from "name" by c7d8e9f0a1b2
