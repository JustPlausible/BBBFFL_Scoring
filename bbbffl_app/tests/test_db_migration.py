"""Proves an existing (pre-competition_key) scorer_decisions.db -- as
already deployed for the live Grand Final trial -- upgrades in place without
losing any recorded decision, and continues to be reachable under the
Grand Final's fixed competition_key afterwards.
"""

import sqlite3

from app.db import DecisionsRepository, init_db

LEGACY_SCHEMA = """
CREATE TABLE slot_dnp (
    team_key TEXT NOT NULL,
    slot TEXT NOT NULL,
    dnp INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (team_key, slot)
);

CREATE TABLE interchange_assignment (
    team_key TEXT PRIMARY KEY,
    target_position TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE score_override (
    team_key TEXT NOT NULL,
    position TEXT NOT NULL,
    override_score REAL,
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (team_key, position)
);

CREATE TABLE matchup_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    finalized INTEGER NOT NULL DEFAULT 0,
    finalized_at TEXT,
    finalized_note TEXT,
    finalized_snapshot TEXT
);
"""


def _legacy_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO slot_dnp (team_key, slot, dnp, updated_at) VALUES ('team_a', 'Forward1', 1, 't')"
    )
    conn.execute(
        "INSERT INTO interchange_assignment (team_key, target_position, updated_at) "
        "VALUES ('team_a', 'Ruck', 't')"
    )
    conn.execute(
        "INSERT INTO score_override (team_key, position, override_score, reason, updated_at) "
        "VALUES ('team_a', 'Ruck', 42.0, 'legacy correction', 't')"
    )
    conn.execute(
        "INSERT INTO matchup_state (id, finalized, finalized_at, finalized_note) "
        "VALUES (1, 1, '2026-01-01T00:00:00+00:00', 'legacy signoff')"
    )
    conn.commit()
    return conn


def test_legacy_dnp_survives_migration_under_the_grand_final_key():
    conn = _legacy_conn()
    init_db(conn)
    repo = DecisionsRepository(conn)  # defaults to grand_final
    assert repo.get_dnp_map()[("team_a", "Forward1")] is True


def test_legacy_interchange_assignment_survives_migration():
    conn = _legacy_conn()
    init_db(conn)
    repo = DecisionsRepository(conn)
    assert repo.get_interchange_assignments()["team_a"].target_position == "Ruck"


def test_legacy_override_survives_migration():
    conn = _legacy_conn()
    init_db(conn)
    repo = DecisionsRepository(conn)
    override = repo.get_overrides()[("team_a", "Ruck")]
    assert override.override_score == 42.0
    assert override.reason == "legacy correction"


def test_legacy_finalized_matchup_state_survives_migration():
    conn = _legacy_conn()
    init_db(conn)
    repo = DecisionsRepository(conn)
    state = repo.get_matchup_state()
    assert state.finalized is True
    assert state.finalized_note == "legacy signoff"


def test_migration_is_idempotent_across_repeated_init_db_calls():
    conn = _legacy_conn()
    init_db(conn)
    init_db(conn)  # simulates a second app startup against the same file
    repo = DecisionsRepository(conn)
    assert repo.get_dnp_map()[("team_a", "Forward1")] is True
    assert repo.get_matchup_state().finalized is True


def test_a_new_competition_key_on_the_migrated_db_starts_clean():
    conn = _legacy_conn()
    init_db(conn)
    superscore = DecisionsRepository(conn, competition_key="superscore:2026:20")
    assert superscore.get_dnp_map() == {}
    assert superscore.get_matchup_state().finalized is False
