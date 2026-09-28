"""Test-only SQLite setup that keeps the suite off the disk's sync path (#218).

Issue #218's CI evidence (PR #217's ``219bed2`` run and PR #258's run) shows
the extreme slowdowns happen on GitHub-hosted runners with throttled or
contended disks: the pytest process sits at ~8% CPU with ~23% iowait while
every *database-backed* test file runs ~6x slower and CPU-only files are not
slower at all. The suite amplifies that, because almost every test builds
its database the expensive way:

* ``tests/db_helpers.migrated_connection`` and the HTTP fixtures (through
  app startup, ``app.main``'s lifespan) create an empty SQLite file and run
  the *entire* Alembic history on it -- ~1,600 fresh migrations per full
  run, each ~1,000 ``fsync`` calls and ~3,800 writes (strace), ~70% of the
  suite's runtime;
* every test's own commits then ``fsync`` again (~27% of the suite's
  ``fsync`` calls in a traced sample).

This module removes both from ordinary tests, without changing what any test
asserts about the application:

1. **Pre-migrated template.** The first time a test needs a fresh
   current-schema SQLite database, the real ``app.migrations.migrate`` runs
   once, into a session-scoped template file. Every later *fresh, to-head*
   upgrade of an empty SQLite file is satisfied by copying that template
   into place instead of replaying the whole history. Each test still gets
   its own private database file, and every caller still goes through the
   real ``migrate()`` (legacy detection included); only the final
   ``alembic.command.upgrade(cfg, "head")`` on an empty file is replaced.

   The real Alembic path is kept for everything else: an explicit or older
   revision, a non-empty database (legacy bootstrap, partial upgrades,
   idempotent re-runs), downgrades, a caller-supplied connection,
   PostgreSQL, and every test marked ``@pytest.mark.real_migrations``
   (``tests/test_db_migration.py`` is, as a whole module).

2. **No fsync for test databases.** Every SQLite connection opened during
   the test session gets ``PRAGMA synchronous = OFF``. SQLite documents
   this as affecting durability only if the *operating system* crashes or
   loses power; transactions, rollback journals, locking, isolation and
   constraint enforcement are unchanged, and test databases are disposable.
   Production uses PostgreSQL; nothing here is imported by the application.

Opt-outs, to reproduce the pre-#218 behaviour exactly (e.g. when checking
whether a failure could be template-related):

* ``BBBFFL_TEST_DB_TEMPLATE=0`` -- every fresh database runs real Alembic;
* ``BBBFFL_TEST_SQLITE_SYNCHRONOUS=FULL`` (or ``NORMAL``) -- keep fsync.

See docs/ci-quality-gates.md ("Python test runtime and slow CI runs").
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field

from alembic import command
from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url

MARKER = "real_migrations"
TEMPLATE_ENV = "BBBFFL_TEST_DB_TEMPLATE"
SYNCHRONOUS_ENV = "BBBFFL_TEST_SQLITE_SYNCHRONOUS"
_SYNCHRONOUS_VALUES = {"OFF", "NORMAL", "FULL", "EXTRA"}


@dataclass
class TemplateStats:
    """What the session did, reported in the terminal summary."""

    template_build_seconds: float | None = None
    clones: int = 0
    real_upgrades_fresh: int = 0
    real_upgrades_other: int = 0
    clone_seconds: float = 0.0
    real_upgrade_seconds: float = 0.0
    reasons: dict[str, int] = field(default_factory=dict)

    def count_real(self, reason: str, fresh: bool, seconds: float) -> None:
        if fresh:
            self.real_upgrades_fresh += 1
        else:
            self.real_upgrades_other += 1
        self.real_upgrade_seconds += seconds
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


class MigratedSQLiteTemplate:
    """Session-scoped, lazily built, read-only migrated SQLite template."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.stats = TemplateStats()
        self.real_migrations_requested = False
        self._real_upgrade = command.upgrade
        self._installed = None
        self._lock = threading.RLock()
        self._building = False
        self._directory: str | None = None
        self.path: str | None = None
        self.sha256: str | None = None

    # -- installation -----------------------------------------------------
    def install(self) -> None:
        # Keep the exact object installed: every ``self._upgrade`` access
        # creates a new bound method, so an identity check against it would
        # never match and teardown would leave the wrapper in place.
        self._installed = self._upgrade
        command.upgrade = self._installed

    def uninstall(self) -> None:
        if self._installed is not None and command.upgrade is self._installed:
            command.upgrade = self._real_upgrade
        self._installed = None
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None
            self.path = None

    # -- decision -----------------------------------------------------------
    def _clone_target(self, config, revision, args, kwargs) -> tuple[str | None, str]:
        """Return (path, "") when this upgrade may be served by the template,
        else (None, reason it must run for real)."""
        from app.migrations import HEAD

        if not self.enabled:
            return None, "template disabled"
        if self._building:
            return None, "template build"
        if self.real_migrations_requested:
            return None, f"@pytest.mark.{MARKER}"
        if args or kwargs:
            return None, "non-default upgrade options"
        if revision not in ("head", HEAD):
            return None, "explicit revision"
        if config.attributes.get("connection") is not None:
            return None, "caller-supplied connection"
        raw_url = config.get_main_option("sqlalchemy.url")
        if not raw_url:
            return None, "no url"
        url = make_url(raw_url)
        if url.get_backend_name() != "sqlite":
            return None, "not sqlite"
        database = url.database
        if not database or database == ":memory:" or database.startswith("file:") or url.query:
            return None, "not a plain sqlite file"
        path = os.path.abspath(database)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return None, "existing database"
        if any(os.path.exists(path + suffix) for suffix in ("-journal", "-wal", "-shm")):
            return None, "sqlite sidecar present"
        return path, ""

    # -- the patched alembic.command.upgrade ---------------------------------
    def _upgrade(self, config, revision, *args, **kwargs):
        target, reason = self._clone_target(config, revision, args, kwargs)
        if target is None:
            fresh = self._is_fresh(config)
            started = time.perf_counter()
            try:
                return self._real_upgrade(config, revision, *args, **kwargs)
            finally:
                self.stats.count_real(reason, fresh, time.perf_counter() - started)
        started = time.perf_counter()
        self.clone_to(target)
        self.stats.clones += 1
        self.stats.clone_seconds += time.perf_counter() - started
        return None

    @staticmethod
    def _is_fresh(config) -> bool:
        raw_url = config.get_main_option("sqlalchemy.url") or ""
        try:
            url = make_url(raw_url)
        except Exception:
            return False
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            return False
        path = os.path.abspath(url.database)
        return not os.path.exists(path) or os.path.getsize(path) == 0

    # -- template ---------------------------------------------------------
    def ensure_built(self) -> str:
        with self._lock:
            if self.path is not None:
                return self.path
            from app.migrations import HEAD, migrate

            directory = tempfile.mkdtemp(prefix="bbbffl-migrated-template-")
            path = os.path.join(directory, "template.db")
            started = time.perf_counter()
            self._building = True
            try:
                # The full real path, exactly as a fresh deployment runs it.
                migrate(f"sqlite:///{path}")
            finally:
                self._building = False
            self.stats.template_build_seconds = time.perf_counter() - started
            _verify_quiescent_head(path, HEAD)
            os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            self._directory = directory
            self.sha256 = _sha256(path)
            self.path = path
            return path

    def clone_to(self, target: str) -> None:
        source = self.ensure_built()
        # copyfile writes the bytes into ``target`` (replacing an empty
        # mkstemp placeholder in place) and never copies the template's
        # read-only mode bits.
        shutil.copyfile(source, target)
        if os.path.getsize(target) != os.path.getsize(source):
            raise RuntimeError(f"migrated SQLite template copy to {target} is incomplete")

    def unchanged(self) -> bool:
        return self.path is None or _sha256(self.path) == self.sha256


def _verify_quiescent_head(path: str, head: str) -> None:
    import sqlite3

    for suffix in ("-journal", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            raise RuntimeError(f"migrated SQLite template left a {suffix} file behind; refusing to copy it")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        versions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    if versions != [(head,)]:
        raise RuntimeError(f"migrated SQLite template is at {versions}, expected {head}")
    if journal_mode.lower() == "wal":
        raise RuntimeError("migrated SQLite template is in WAL mode; a plain file copy would not be safe")


def _sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# -- synchronous pragma -------------------------------------------------------
def synchronous_setting() -> str:
    value = os.getenv(SYNCHRONOUS_ENV, "OFF").strip().upper()
    if value not in _SYNCHRONOUS_VALUES:
        raise ValueError(f"{SYNCHRONOUS_ENV} must be one of {sorted(_SYNCHRONOUS_VALUES)}, not {value!r}")
    return value


def _make_synchronous_listener(value: str):
    def set_synchronous(dbapi_connection, _record):
        # Only SQLite; PostgreSQL connections are left exactly as they are.
        if type(dbapi_connection).__module__.startswith("sqlite3"):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"PRAGMA synchronous = {value}")
            finally:
                cursor.close()

    return set_synchronous


# -- pytest wiring (called from tests/conftest.py) ------------------------------
TEMPLATE: MigratedSQLiteTemplate | None = None
_LISTENER = None


def configure(config) -> None:
    global TEMPLATE, _LISTENER
    config.addinivalue_line(
        "markers",
        f"{MARKER}: run real Alembic migrations for fresh SQLite databases in this test instead of copying "
        "the session's pre-migrated template (issue #218)",
    )
    TEMPLATE = MigratedSQLiteTemplate(enabled=os.getenv(TEMPLATE_ENV, "1").strip() not in ("0", "false", "no"))
    TEMPLATE.install()
    _LISTENER = _make_synchronous_listener(synchronous_setting())
    event.listen(Engine, "connect", _LISTENER)


def unconfigure() -> None:
    global TEMPLATE, _LISTENER
    if _LISTENER is not None and event.contains(Engine, "connect", _LISTENER):
        event.remove(Engine, "connect", _LISTENER)
    _LISTENER = None
    if TEMPLATE is not None:
        TEMPLATE.uninstall()
    TEMPLATE = None


def summary_line() -> str | None:
    if TEMPLATE is None:
        return None
    s = TEMPLATE.stats
    build = "not needed" if s.template_build_seconds is None else f"built once in {s.template_build_seconds:.2f}s"
    reasons = ", ".join(f"{reason}: {count}" for reason, count in sorted(s.reasons.items())) or "none"
    return (
        f"[db-setup] migrated SQLite template {build}"
        f"{'' if TEMPLATE.enabled else ' (DISABLED via ' + TEMPLATE_ENV + ')'}; "
        f"{s.clones} fresh databases cloned in {s.clone_seconds:.1f}s; "
        f"real Alembic upgrades: {s.real_upgrades_fresh} fresh + {s.real_upgrades_other} other "
        f"in {s.real_upgrade_seconds:.1f}s ({reasons}); sqlite synchronous={synchronous_setting()}"
    )
