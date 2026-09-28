"""Regression coverage for the test-only migrated SQLite template (#218).

tests/sqlite_test_template.py replaces the *final* fresh-to-head Alembic
upgrade of an empty SQLite file with a copy of one real, session-built
migrated template, and turns off fsync for test SQLite connections. These
tests pin what that must never change: a cloned database is equivalent to a
real fresh migration, every test's database is private, the shared template
is never written, and everything that is about migrations still runs real
Alembic.
"""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.db import connect, transaction
from app.migrations import HEAD, migrate
from app.season import SeasonRepository
from tests import sqlite_test_template
from tests.db_helpers import migrated_connection


@pytest.fixture
def template(monkeypatch):
    """The session's template, enabled for this test.

    These tests exercise the template mechanism itself, so they must keep
    working when the whole session runs with the documented
    ``BBBFFL_TEST_DB_TEMPLATE=0`` opt-out (the template is then built on
    demand here, and still removed at session end).
    """
    active = sqlite_test_template.TEMPLATE
    assert active is not None, "tests/conftest.py must install the migrated SQLite template"
    monkeypatch.setattr(active, "enabled", True)
    active.ensure_built()
    return active


def _empty_sqlite_path() -> Path:
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    return Path(path)


def _normalised_schema(path: Path) -> dict:
    """sqlite_master with each statement's clauses sorted.

    Alembic's SQLite batch mode recreates tables from reflected constraints,
    whose order varies between runs (two *real* fresh migrations already
    differ textually), so equality is on the set of clauses, not the text.
    """
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    finally:
        connection.close()
    return {
        (kind, name, table): sorted(line.strip().rstrip(",") for line in (sql or "").splitlines())
        for kind, name, table, sql in rows
    }


def _all_rows(path: Path) -> dict:
    connection = sqlite3.connect(path)
    try:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {table: sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall()) for table in tables}
    finally:
        connection.close()


def _file_pragmas(path: Path) -> dict:
    connection = sqlite3.connect(path)
    try:
        return {
            name: connection.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("journal_mode", "page_size", "user_version", "application_id", "auto_vacuum", "encoding")
        }
    finally:
        connection.close()


def test_cloned_database_is_equivalent_to_a_real_fresh_migration(template, monkeypatch):
    cloned = _empty_sqlite_path()
    clones_before = template.stats.clones
    migrate(f"sqlite:///{cloned}")
    assert template.stats.clones == clones_before + 1

    real = _empty_sqlite_path()
    fresh_before = template.stats.real_upgrades_fresh
    monkeypatch.setattr(template, "real_migrations_requested", True)
    migrate(f"sqlite:///{real}")
    assert template.stats.real_upgrades_fresh == fresh_before + 1

    assert _normalised_schema(cloned) == _normalised_schema(real)
    assert _all_rows(cloned) == _all_rows(real) == {**_all_rows(real), "alembic_version": [(HEAD,)]}
    assert _file_pragmas(cloned) == _file_pragmas(real)


def test_each_fresh_database_is_private_and_the_template_is_never_written(template):
    first, second = migrated_connection(), migrated_connection()
    first_path = Path(first.engine.url.database)
    second_path = Path(second.engine.url.database)
    assert first_path != second_path
    assert template.path not in (str(first_path), str(second_path))

    SeasonRepository(first).create_season(2031, "Only in the first database")

    assert [row["year"] for row in first.execute("SELECT year FROM bbbffl_season").fetchall()] == [2031]
    assert second.execute("SELECT year FROM bbbffl_season").fetchall() == []
    assert template.unchanged()
    assert not os.access(template.path, os.W_OK) or os.geteuid() == 0  # read-only (root ignores mode bits)
    assert _all_rows(Path(template.path))["bbbffl_season"] == []
    first.close()
    second.close()


@pytest.mark.real_migrations
def test_real_migrations_marker_runs_real_alembic_for_a_fresh_database(template):
    path = _empty_sqlite_path()
    clones_before, fresh_before = template.stats.clones, template.stats.real_upgrades_fresh
    migrate(f"sqlite:///{path}")
    assert template.stats.clones == clones_before
    assert template.stats.real_upgrades_fresh == fresh_before + 1
    assert _all_rows(path)["alembic_version"] == [(HEAD,)]


def test_explicit_revisions_and_existing_databases_still_run_real_alembic(template):
    path = _empty_sqlite_path()
    url = f"sqlite:///{path}"
    clones_before, other_before = template.stats.clones, template.stats.real_upgrades_other

    migrate(url, "0027_midseason_draft")  # explicit revision: real, even on an empty file
    assert _all_rows(path)["alembic_version"] == [("0027_midseason_draft",)]
    migrate(url)  # existing, partially migrated database: the real remaining upgrades
    assert _all_rows(path)["alembic_version"] == [(HEAD,)]
    migrate(url)  # already at head: a real (no-op) upgrade, never a re-clone over data

    assert template.stats.clones == clones_before
    assert template.stats.real_upgrades_other >= other_before + 2


def test_a_database_with_data_is_never_replaced_by_the_template(template):
    connection = migrated_connection()
    SeasonRepository(connection).create_season(2032, "Must survive a re-run of migrate")
    clones_before = template.stats.clones

    migrate(str(connection.engine.url))

    assert template.stats.clones == clones_before
    assert [row["year"] for row in connection.execute("SELECT year FROM bbbffl_season").fetchall()] == [2032]
    connection.close()


def test_a_leftover_sqlite_journal_forces_the_real_path(template, monkeypatch):
    path = _empty_sqlite_path()
    Path(f"{path}-journal").write_bytes(b"")
    clones_before = template.stats.clones
    try:
        migrate(f"sqlite:///{path}")
    finally:
        Path(f"{path}-journal").unlink(missing_ok=True)
    assert template.stats.clones == clones_before
    assert _all_rows(path)["alembic_version"] == [(HEAD,)]


def test_disabling_the_template_restores_real_fresh_migrations(template, monkeypatch):
    monkeypatch.setattr(template, "enabled", False)
    path = _empty_sqlite_path()
    clones_before, fresh_before = template.stats.clones, template.stats.real_upgrades_fresh
    migrate(f"sqlite:///{path}")
    assert template.stats.clones == clones_before
    assert template.stats.real_upgrades_fresh == fresh_before + 1


def test_app_startup_on_an_empty_database_still_runs_migrate_and_serves_the_current_schema(template, monkeypatch):
    path = _empty_sqlite_path()
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    clones_before = template.stats.clones

    from app.main import app

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        database = client.app.state.database
        assert database.execute("SELECT version_num FROM alembic_version").fetchall()[0]["version_num"] == HEAD

    assert template.stats.clones == clones_before + 1
    path.unlink(missing_ok=True)


def test_test_sqlite_connections_skip_fsync_but_keep_foreign_keys_journal_and_rollback():
    connection = migrated_connection()
    # OFF by default; the documented BBBFFL_TEST_SQLITE_SYNCHRONOUS opt-out
    # must be honoured exactly as configured.
    expected = {"OFF": 0, "NORMAL": 1, "FULL": 2, "EXTRA": 3}[sqlite_test_template.synchronous_setting()]
    assert connection.execute("PRAGMA synchronous").fetchone()["synchronous"] == expected
    assert connection.execute("PRAGMA foreign_keys").fetchone()["foreign_keys"] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()["journal_mode"] == "delete"

    with pytest.raises(RuntimeError):
        with transaction(connection) as tx:
            tx.execute(
                "INSERT INTO slot_dnp (competition_key, team_key, slot, dnp, updated_at) "
                "VALUES ('k', 'team', 'Forward1', 1, 't')"
            )
            assert tx.execute("SELECT COUNT(*) AS n FROM slot_dnp").fetchone()["n"] == 1
            raise RuntimeError("roll back")
    assert connection.execute("SELECT COUNT(*) AS n FROM slot_dnp").fetchone()["n"] == 0
    connection.close()


def test_postgresql_style_urls_are_never_served_by_the_template(template):
    from alembic.config import Config

    config = Config()
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:pw@localhost/db")
    assert template._clone_target(config, "head", (), {}) == (None, "not sqlite")
    memory = Config()
    memory.set_main_option("sqlalchemy.url", "sqlite:///:memory:")
    assert template._clone_target(memory, "head", (), {})[0] is None


def test_the_migration_history_module_keeps_running_real_alembic():
    import tests.test_db_migration as migration_tests

    marks = migration_tests.pytestmark if isinstance(migration_tests.pytestmark, list) else [migration_tests.pytestmark]
    assert sqlite_test_template.MARKER in {mark.name for mark in marks}


def test_connect_is_unaffected_for_an_already_migrated_file(template):
    # A helper or fixture that opens an existing migrated file directly
    # (without migrate) sees exactly that file, not the template.
    path = _empty_sqlite_path()
    migrate(f"sqlite:///{path}")
    connection = connect(f"sqlite:///{path}")
    SeasonRepository(connection).create_season(2034, "Direct connect")
    assert _all_rows(Path(template.path))["bbbffl_season"] == []
    connection.close()


def test_uninstall_restores_exactly_the_upgrade_function_it_replaced():
    # e.g. pytest.main() inside a longer-lived process: teardown must put
    # alembic.command.upgrade back rather than leave a template wrapper.
    before = command.upgrade
    extra = sqlite_test_template.MigratedSQLiteTemplate(enabled=True)
    extra.install()
    assert command.upgrade is not before
    extra.uninstall()
    assert command.upgrade is before
    extra.uninstall()  # idempotent
    assert command.upgrade is before
