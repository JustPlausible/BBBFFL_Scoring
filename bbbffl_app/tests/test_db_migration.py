"""Regression coverage for the real Alembic history and legacy bootstrap."""
import json
import sqlite3

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.db import DecisionsRepository, connect
from app.migrations import HEAD, downgrade, migrate
from app.round_mapping import RoundMappingRepository
from app.season import SeasonRepository

LEGACY_SCHEMA = """
CREATE TABLE slot_dnp (team_key TEXT NOT NULL, slot TEXT NOT NULL, dnp INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY (team_key, slot));
CREATE TABLE interchange_assignment (team_key TEXT PRIMARY KEY, target_position TEXT, updated_at TEXT NOT NULL);
CREATE TABLE score_override (team_key TEXT NOT NULL, position TEXT NOT NULL, override_score REAL, reason TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (team_key, position));
CREATE TABLE matchup_state (id INTEGER PRIMARY KEY CHECK (id = 1), finalized INTEGER NOT NULL DEFAULT 0, finalized_at TEXT, finalized_note TEXT, finalized_snapshot TEXT);
"""
EXPECTED_TABLES = {
    "alembic_version",
    "slot_dnp",
    "interchange_assignment",
    "score_override",
    "matchup_state",
    "audit_event",
    "bbbffl_season",
    "season_rules_version",
    "competition_stream",
    "bbbffl_round",
    "round_afl_mapping",
    "round_afl_mapping_revision",
    "coach",
    "season_entry",
    "season_entry_coach_history",
    "season_entry_team_name_history",
    "season_player_pool",
    "season_squad_configuration",
    "player_ownership_period",
    "season_fixture_draw",
    "season_fixture_number",
    "season_fixture_matchup",
}


def _url(path):
    return f"sqlite:///{path}"


def _legacy_database(path):
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO slot_dnp VALUES ('team_a', 'Forward1', 1, 't')")
    conn.execute("INSERT INTO interchange_assignment VALUES ('team_a', 'Ruck', 't')")
    conn.execute("INSERT INTO score_override VALUES ('team_a', 'Ruck', 42.0, 'legacy correction', 't')")
    snapshot = json.dumps({"status": "FINAL", "teams": [{"team_key": "team_a", "total_score": 99}]})
    conn.execute("INSERT INTO matchup_state VALUES (1, 1, '2026-01-01T00:00:00+00:00', 'legacy signoff', ?)", (snapshot,))
    conn.commit()
    # Prove this fixture is genuinely unversioned and meaningful before migration.
    assert conn.execute("SELECT dnp FROM slot_dnp").fetchone()[0] == 1
    assert "alembic_version" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()


def test_empty_database_migrates_to_head_and_repository_works(tmp_path):
    url = _url(tmp_path / "fresh.db")
    migrate(url)
    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
        # Composite primary keys create backing indexes on SQLite.
        assert inspect(conn).get_pk_constraint("slot_dnp")["constrained_columns"] == ["competition_key", "team_key", "slot"]
    repo_conn = connect(url)
    repo = DecisionsRepository(repo_conn)
    repo.set_dnp("team_a", "Forward1", True)
    assert repo.get_dnp_map() == {("team_a", "Forward1"): True}
    repo_conn.close()


def test_realistic_legacy_state_survives_losslessly(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_database(path)
    url = _url(path)
    migrate(url)
    repo = DecisionsRepository(connect(url))
    assert repo.get_dnp_map() == {("team_a", "Forward1"): True}
    assert repo.get_interchange_assignments()["team_a"].target_position == "Ruck"
    assert repo.get_overrides()[("team_a", "Ruck")].override_score == 42.0
    state = repo.get_matchup_state()
    assert state.finalized and state.finalized_note == "legacy signoff"
    assert state.snapshot["teams"][0]["total_score"] == 99


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_database(path)
    url = _url(path)
    migrate(url)
    migrate(url)
    assert DecisionsRepository(connect(url)).get_dnp_map() == {("team_a", "Forward1"): True}


def test_upgrade_from_previous_head_preserves_scorer_state(tmp_path):
    url = _url(tmp_path / "previous-head.db")
    migrate(url, "0003_audit")
    repo = DecisionsRepository(connect(url))
    repo.set_dnp("team_a", "Forward1", True)
    repo.finalize("signed", {"status": "FINAL"})
    before_migration = repo.get_matchup_state()
    migrate(url)
    restored = DecisionsRepository(connect(url))
    after_migration = restored.get_matchup_state()
    assert restored.get_dnp_map() == {("team_a", "Forward1"): True}
    assert after_migration.snapshot == before_migration.snapshot
    assert after_migration.snapshot["status"] == "FINAL"
    assert after_migration.snapshot["finalized_note"] == "signed"
    assert after_migration.snapshot["finalized_at"]


def test_upgrade_from_season_head_and_empty_identity_downgrade(tmp_path):
    url = _url(tmp_path / "identity-upgrade.db")
    migrate(url, "0004_season")
    migrate(url)
    engine = create_engine(url)
    assert "season_entry" in set(inspect(engine).get_table_names())
    downgrade(url, "0004_season")
    assert "season_entry" not in set(inspect(create_engine(url)).get_table_names())


def test_upgrade_from_identity_head_adds_player_ownership_schema(tmp_path):
    url = _url(tmp_path / "player-upgrade.db")
    migrate(url, "0005_identity")
    migrate(url)
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"season_player_pool", "season_squad_configuration", "player_ownership_period"} <= tables


def test_upgrade_from_player_head_adds_fixture_schema(tmp_path):
    url = _url(tmp_path / "fixture-upgrade.db")
    migrate(url, "0006_players")
    migrate(url)
    assert {"season_fixture_draw", "season_fixture_number", "season_fixture_matchup"} <= set(inspect(create_engine(url)).get_table_names())


def test_upgrade_from_0007_preserves_legacy_round_mapping(tmp_path):
    url = _url(tmp_path / "round-mapping-upgrade.db")
    migrate(url, "0007_fixture")
    connection = connect(url)
    seasons = SeasonRepository(connection)
    season = seasons.create_season(2026, "2026 Replay")
    rules = seasons.create_rules_version(season.season_id, "canonical", 1, "2026")
    competition = seasons.create_competition(
        season.season_id, rules.rules_version_id, "ordinary", "BBBFFL", "ordinary"
    )
    round_ = seasons.create_round(competition.competition_id, "r1", "Round 1", 1)
    legacy_id = "legacy-mapping-id"
    with connection.engine.begin() as raw:
        raw.execute(
            text(
                "INSERT INTO bbbffl_round_afl_reference VALUES "
                "(:mapping, :round, :provider, :season, :afl_round, :created)"
            ),
            {
                "mapping": legacy_id,
                "round": round_.bbbffl_round_id,
                "provider": "afl-api-v1",
                "season": 84,
                "afl_round": 1300,
                "created": "2026-03-01T00:00:00+00:00",
            },
        )
    connection.close()

    migrate(url)
    migrate(url)  # repeated upgrades remain harmless
    upgraded = connect(url)
    resolved = RoundMappingRepository(upgraded).resolve(round_.bbbffl_round_id)
    assert resolved is not None
    assert resolved.mapping_id == legacy_id
    assert (resolved.afl_season_id, resolved.afl_round_id) == (84, 1300)
    assert "bbbffl_round_afl_reference" not in set(inspect(upgraded.engine).get_table_names())


def test_unrecognized_unversioned_schema_is_refused(tmp_path):
    path = tmp_path / "unknown.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE slot_dnp (surprise TEXT)")
    conn.close()
    with pytest.raises(RuntimeError, match="Unrecognized BBBFFL schema"):
        migrate(_url(path))


def test_reversible_downgrade_preserves_representable_grand_final_data(tmp_path):
    # Uses raw SQL rather than DecisionsRepository: the repository always
    # appends an audit event (see app/audit.py), and 0003's downgrade
    # refuses once audit_event holds history -- that refusal is exercised
    # separately below. This test isolates the pre-existing 0002->0001
    # column-shape preservation the downgrade chain must still honour.
    url = _url(tmp_path / "down.db")
    migrate(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO slot_dnp (competition_key, team_key, slot, dnp, updated_at) "
                "VALUES ('grand_final', 'team_a', 'Forward1', 1, '2026-01-01T00:00:00+00:00')"
            )
        )
    downgrade(url, "0001_prototype")
    engine = create_engine(url)
    with engine.connect() as conn:
        assert {c["name"] for c in inspect(conn).get_columns("slot_dnp")} == {"team_key", "slot", "dnp", "updated_at"}
        assert conn.execute(text("SELECT dnp FROM slot_dnp WHERE team_key='team_a'")).scalar_one() == 1


def test_downgrade_refuses_loss_of_other_competitions(tmp_path):
    url = _url(tmp_path / "irreversible.db")
    migrate(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO slot_dnp (competition_key, team_key, slot, dnp, updated_at) "
                "VALUES ('superscore:2026:20', 'team_a', 'Forward1', 1, '2026-01-01T00:00:00+00:00')"
            )
        )
    with pytest.raises(RuntimeError, match="cannot be represented"):
        downgrade(url, "0001_prototype")


def test_downgrade_refuses_loss_of_audit_history(tmp_path):
    url = _url(tmp_path / "audit-irreversible.db")
    migrate(url)
    repo = DecisionsRepository(connect(url))
    repo.set_dnp("team_a", "Forward1", True)
    repo.conn.close()
    with pytest.raises(RuntimeError, match="audit_event holds history"):
        downgrade(url, "0002_competition")


def test_downgrade_to_0002_succeeds_when_audit_event_is_empty(tmp_path):
    url = _url(tmp_path / "audit-empty.db")
    migrate(url)
    downgrade(url, "0002_competition")
    engine = create_engine(url)
    assert "audit_event" not in set(inspect(engine).get_table_names())


def test_revision_chain_has_single_head():
    cfg = Config("alembic.ini")
    assert ScriptDirectory.from_config(cfg).get_heads() == [HEAD]
