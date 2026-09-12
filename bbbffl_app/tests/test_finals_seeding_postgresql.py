"""PostgreSQL-specific regression for issue #187's finals-seeding snapshot:
the `FOR UPDATE`-locked apply path and the database-level immutability
triggers `migrations/versions/0028_finals_seeding.py` installs (mirroring
`tests/test_midseason_draft_postgresql.py`'s own PostgreSQL-only
precedent -- SQLite has neither `FOR UPDATE` nor these triggers' exact
error surface).

`bbbffl_season` has a UNIQUE constraint on `year`, and `app.finals_seeding`
deliberately only ever accepts `year=2026` -- unlike sibling PostgreSQL
suites (e.g. `tests/test_midseason_draft_postgresql.py`'s
`itertools.count(2260)`), this file cannot hand each test its own season
year, so every scenario below shares one 2026 season/apply in a single
test rather than colliding on a repeated `INSERT`.

The CI `postgres-migrations` job's own inline setup step already seeds a
`year=2026` season into the shared `BBBFFL_DATABASE_URL` database before
this suite's pytest step runs (`.github/workflows/ci.yml`'s "Upgrade twice
and exercise scorer persistence and audit boundary" step), so this file
cannot reuse that database directly -- it connects to a dedicated
`<database>_finals_seeding` database instead (created on demand), fully
isolated from every other step/test sharing the base database.
"""

import os

import psycopg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.db import connect
from app.finals_seeding import FinalsSeedingConflictError, FinalsSeedingRepository
from app.identity import IdentityRepository
from app.migrations import migrate
from tests.finals_seeding_helpers import build_2026_replay_season


def _isolated_database_url(base_url: str) -> str:
    """Swap the base URL's database name for a dedicated
    `<database>_finals_seeding` database, dropped and recreated fresh on
    every call. This suite's hard-coded `year=2026` season must never
    collide with another step or test's own use of the shared base
    database (see module docstring), and must also never accumulate state
    across repeated local runs the way a real, persistent database would
    -- `WITH (FORCE)` drops it even if a prior run's pooled connection is
    still technically open."""
    url = make_url(base_url)
    isolated_name = f"{url.database}_finals_seeding"
    admin_conninfo = psycopg.conninfo.make_conninfo(
        host=url.host, port=url.port or 5432, user=url.username, password=url.password, dbname="postgres"
    )
    with psycopg.connect(admin_conninfo, autocommit=True) as admin_connection:
        admin_connection.execute(f'DROP DATABASE IF EXISTS "{isolated_name}" WITH (FORCE)')
        admin_connection.execute(f'CREATE DATABASE "{isolated_name}"')
    return url.set(database=isolated_name).render_as_string(hide_password=False)


@pytest.fixture
def postgres_database():
    base_url = os.getenv("BBBFFL_DATABASE_URL")
    if not base_url or not base_url.startswith("postgresql"):
        pytest.skip("PostgreSQL finals-seeding regression requires BBBFFL_DATABASE_URL")
    url = _isolated_database_url(base_url)
    migrate(url)
    database = connect(url)
    yield database
    database.close()


def test_postgresql_apply_idempotent_repeat_conflict_and_immutability(postgres_database):
    ctx = build_2026_replay_season(database=postgres_database)
    repo = FinalsSeedingRepository(ctx["database"])

    # Apply, then an idempotent repeat -- both under real `FOR UPDATE` locks.
    first = repo.apply(ctx["season"].season_id, ctx["competition"].competition_id, reason="postgresql regression")
    second = repo.apply(ctx["season"].season_id, ctx["competition"].competition_id, reason="postgresql regression")
    assert first["created"] is True
    assert second["created"] is False
    assert second["snapshot_id"] == first["snapshot_id"]
    assert ctx["database"].execute("SELECT COUNT(*) AS n FROM finals_seeding_snapshot").fetchone()["n"] == 1

    # A changed resolution (two entries' display names swapped) fails
    # closed rather than silently overwriting the existing snapshot.
    identities = IdentityRepository(ctx["database"])
    entries = {
        identities.get_public_team(entry.season_entry_id).team_name: entry.season_entry_id for entry in ctx["entries"]
    }
    running_hots_id, plague_id = entries["Running Hots"], entries["The Plague"]
    identities.rename_team(running_hots_id, "Temp Name", reason="swap")
    identities.rename_team(plague_id, "Running Hots", reason="swap")
    identities.rename_team(running_hots_id, "The Plague", reason="swap")
    with pytest.raises(FinalsSeedingConflictError):
        repo.apply(ctx["season"].season_id, ctx["competition"].competition_id, reason="second pass")
    assert ctx["database"].execute("SELECT COUNT(*) AS n FROM finals_seeding_snapshot").fetchone()["n"] == 1

    # The frozen snapshot tables reject UPDATE and DELETE outright.
    with pytest.raises(DBAPIError, match="immutable"):
        ctx["database"].execute(
            "UPDATE finals_seeding_snapshot SET through_round=1 WHERE snapshot_id=?", (first["snapshot_id"],)
        )
    with pytest.raises(DBAPIError, match="immutable"):
        ctx["database"].execute(
            "UPDATE finals_seeding_snapshot_seed_row SET season_entry_id=season_entry_id "
            "WHERE snapshot_id=? AND seed_position=1",
            (first["snapshot_id"],),
        )
    with pytest.raises(DBAPIError, match="immutable"):
        ctx["database"].execute("DELETE FROM finals_seeding_snapshot WHERE snapshot_id=?", (first["snapshot_id"],))

    # A rejected statement's failed implicit transaction must not poison the
    # connection for the next call -- `DatabaseConnection.execute` uses a
    # fresh pooled connection each time (see ci.yml's own frozen-fixture
    # regression for the identical pattern), and the snapshot must still
    # read back exactly as it did before the rejected mutations.
    assert ctx["database"].execute("SELECT 1 AS usable").fetchone()["usable"] == 1
    unchanged = repo.get_snapshot(ctx["season"].season_id)
    assert unchanged.snapshot_id == first["snapshot_id"]
    assert [(row.seed_position, row.season_entry_id) for row in unchanged.seed_rows] == first["seed_positions"]
