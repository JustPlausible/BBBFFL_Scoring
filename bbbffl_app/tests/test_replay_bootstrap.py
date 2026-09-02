import json

import pytest

from app.auth import AuthenticationService, CredentialRepository, SessionRepository
from app.auth_rate_limit import LoginRateLimiter
from app.draft import DraftRepository
from app.draft_board import draft_board_readiness
from app.identity import IdentityRepository
from app.player_pool import PlayerPoolRepository
from app.replay_bootstrap import (
    ReplayBootstrapError,
    bootstrap_first_half,
    load_replay_config,
    provision_replay_operator,
    replay_readiness,
)
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


def _files(tmp_path, *, entry_count=10, mutate_entry=None, mutate_player=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "coach_display_name": f"Historical Coach {n}",
            "coach_email": f"coach{n}@replay.example",
            "team_name": f"Historical Club {n}",
            "licence_key": f"historical-{n}",
            "draft_position": n,
        }
        for n in range(1, entry_count + 1)
    ]
    if mutate_entry:
        mutate_entry(entries)
    players = [
        {
            "canonical_player_id": 2026000 + n,
            "display_name": f"Captured Player {n}",
            "afl_team_id": 1000 + (n % 18),
            "afl_team_name": f"AFL Club {n % 18}",
            "eligible": True,
            "source_updated_at": "2026-02-01T00:00:00Z",
        }
        for n in range(1, 31)
    ]
    if mutate_player:
        mutate_player(players)
    (tmp_path / "players.json").write_text(
        json.dumps({"source": {"provider": "afl-api-v1", "season_year": 2026}, "players": players})
    )
    config = {
        "season": {"year": 2026, "label": "2026 First Half Replay"},
        "rules": {"key": "ordinary", "version": 1, "name": "2026 Rules"},
        "competition": {"key": "ordinary", "label": "BBBFFL Ordinary"},
        "squad_limit": 3,
        "operator_email": "coach1@replay.example",
        "player_pool_file": "players.json",
        "entries": entries,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def test_clean_bootstrap_is_ready_for_human_pick_one_and_season_centre_state(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    initial = bootstrap_first_half(database, config)
    assert initial["overall"] == "NOT READY"
    assert initial["checks"]["operator_authentication_provisioned"] is False
    provision_replay_operator(database, config, "correct horse battery staple")
    report = replay_readiness(database, config)
    season = SeasonRepository(database).get_season_by_year(2026)
    competitions = SeasonRepository(database).list_competitions(season.season_id)

    assert report["overall"] == "READY"
    assert report["logical_rounds"] == list(range(1, 10))
    assert report["season_entry_count"] == report["accepted_draft_order_count"] == 10
    assert report["player_pool_count"] == 30
    assert report["squad_size_limit"] == 3
    assert report["completed_draft_picks_exist"] is False
    assert report["next_human_action"] == "Pick 1"
    assert report["operator_authentication_provisioned"] is True
    assert report["checks"]["exact_rules_version"] is True
    assert len(competitions) == 1 and competitions[0].stream_type == "ordinary"
    assert len(SeasonRepository(database).list_rounds(competitions[0].competition_id)) == 9
    assert len(IdentityRepository(database).list_entries(season.season_id)) == 10
    assert DraftRepository(database).status(season.season_id).completed_picks == 0
    assert (
        draft_board_readiness(
            database,
            IdentityRepository(database),
            DraftRepository(database),
            PlayerPoolRepository(database),
            season.season_id,
        )["next_pick_overall"]
        == 1
    )
    auth = AuthenticationService(
        IdentityRepository(database),
        CredentialRepository(database),
        SessionRepository(database),
        LoginRateLimiter(max_attempts=5, lockout_seconds=300),
    )
    assert auth.login(config.operator_email, "correct horse battery staple", remote_addr="127.0.0.1").token


@pytest.mark.parametrize(
    "entry_count,mutation",
    [
        (9, None),
        (11, None),
        (10, lambda rows: rows[1].update(team_name=rows[0]["team_name"])),
        (10, lambda rows: rows[1].update(coach_email=rows[0]["coach_email"])),
        (10, lambda rows: rows[1].update(draft_position=1)),
        (10, lambda rows: rows[1].pop("draft_position")),
        (10, lambda rows: rows[1].update(draft_position=11)),
    ],
)
def test_invalid_entry_and_order_input_fails_before_writes(tmp_path, entry_count, mutation):
    database = migrated_connection()
    with pytest.raises(ReplayBootstrapError):
        load_replay_config(_files(tmp_path, entry_count=entry_count, mutate_entry=mutation))
    assert SeasonRepository(database).list_seasons() == []


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda rows: rows[0].update(canonical_player_id=None), "canonical_player_id"),
        (lambda rows: rows[0].update(afl_team_id=None), "afl_team_id"),
        (lambda rows: rows[1].update(canonical_player_id=rows[0]["canonical_player_id"]), "duplicate"),
    ],
)
def test_unresolved_or_duplicate_player_identity_is_rejected(tmp_path, mutation, match):
    with pytest.raises(ReplayBootstrapError, match=match):
        load_replay_config(_files(tmp_path, mutate_player=mutation))


def test_bootstrap_is_idempotent_and_preserves_provider_identity(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    first = bootstrap_first_half(database, config)
    provision_replay_operator(database, config, "correct horse battery staple")
    first = replay_readiness(database, config)
    second = bootstrap_first_half(database, config)
    season_id = first["season"]["season_id"]
    player = PlayerPoolRepository(database).get(season_id, 2026001)

    assert first["overall"] == second["overall"] == "READY"
    assert player.source_provider == "afl-api-v1"
    assert player.afl_team_id == 1001
    assert database.execute("SELECT COUNT(*) n FROM draft_pick WHERE season_id=?", (season_id,)).fetchone()["n"] == 30
    assert DraftRepository(database).status(season_id).completed_picks == 0


def test_paused_draft_blocks_pick_one_and_rerun_does_not_resume(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]
    draft = DraftRepository(database)
    draft.pause(season_id, reason="operator investigation")

    blocked = replay_readiness(database, config)
    assert blocked["overall"] == "NOT READY"
    assert blocked["draft_board"]["checks"]["draft_not_paused"] is False
    assert blocked["next_human_action"] is None
    with pytest.raises(ReplayBootstrapError, match="operational state conflicts"):
        bootstrap_first_half(database, config)
    assert draft.status(season_id).is_paused is True


def test_non_2026_config_is_rejected_before_writes(tmp_path):
    database = migrated_connection()
    path = _files(tmp_path)
    raw = json.loads(path.read_text())
    raw["season"]["year"] = 2027
    path.write_text(json.dumps(raw))
    pool_path = tmp_path / "players.json"
    pool = json.loads(pool_path.read_text())
    pool["source"]["season_year"] = 2027
    pool_path.write_text(json.dumps(pool))

    with pytest.raises(ReplayBootstrapError, match="only supports the 2026 replay"):
        load_replay_config(path)
    assert SeasonRepository(database).list_seasons() == []


def test_extra_rules_version_conflicts_without_mutating_existing_state(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]
    seasons = SeasonRepository(database)
    seasons.create_rules_version(season_id, "unexpected", 2, "Unexpected Rules")

    blocked = replay_readiness(database, config)
    assert blocked["overall"] == "NOT READY"
    assert blocked["checks"]["exact_rules_version"] is False
    with pytest.raises(ReplayBootstrapError, match="exactly the configured"):
        bootstrap_first_half(database, config)
    assert [(r.rules_key, r.version_number) for r in seasons.list_rules_versions(season_id)] == [
        ("ordinary", 1),
        ("unexpected", 2),
    ]


def test_conflicting_rerun_rolls_back_without_changing_valid_state(tmp_path):
    database = migrated_connection()
    original = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, original)
    season_id = report["season"]["season_id"]
    conflict_path = _files(tmp_path / "conflict", mutate_entry=lambda rows: rows[0].update(team_name="Different Club"))

    with pytest.raises(ReplayBootstrapError, match="conflicts"):
        bootstrap_first_half(database, load_replay_config(conflict_path))
    assert IdentityRepository(database).list_entries(season_id)[0].team_name != "Different Club"
    assert len(DraftRepository(database).order(season_id)) == 10
    assert DraftRepository(database).status(season_id).completed_picks == 0


def test_acquired_player_pool_output_shape_loads_through_bootstrap_config_without_a_mocked_seam(tmp_path):
    """The AFL-api #248 season-player acquisition output and #116's bootstrap
    player-pool input contract must meet without any translation seam beyond
    the one small, explicit mapping scripts/first_half_replay.py performs."""
    from app.replay_acquisition import acquire_first_half_2026
    from tests.test_replay_acquisition import Api, make_players

    payload = acquire_first_half_2026(
        Api(players=make_players(40, start=20), no_roster=True), source_base_url="http://api"
    )
    pool = {
        "source": {"provider": "afl-api-v1", "season_year": 2026},
        "players": [
            {
                "canonical_player_id": x["canonical_player_id"],
                "display_name": x["display_name"],
                "afl_team_id": x["team_id"],
                "afl_team_name": x["team_name"],
                "eligible": True,
                "source_updated_at": None,
            }
            for x in payload["players"]
        ],
    }
    (tmp_path / "players.json").write_text(json.dumps(pool))
    entries = [
        {
            "coach_display_name": f"Historical Coach {n}",
            "coach_email": f"coach{n}@replay.example",
            "team_name": f"Historical Club {n}",
            "licence_key": f"historical-{n}",
            "draft_position": n,
        }
        for n in range(1, 11)
    ]
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "season": {"year": 2026, "label": "2026 First Half Replay"},
                "rules": {"key": "ordinary", "version": 1, "name": "2026 Rules"},
                "competition": {"key": "ordinary", "label": "BBBFFL Ordinary"},
                "squad_limit": 3,
                "operator_email": "coach1@replay.example",
                "player_pool_file": "players.json",
                "entries": entries,
            }
        )
    )

    loaded = load_replay_config(config_path)
    assert loaded.source_provider == "afl-api-v1"
    assert loaded.source_season_year == 2026
    assert {p.canonical_player_id for p in loaded.players} == {p["canonical_player_id"] for p in payload["players"]}
    assert all(p.eligible for p in loaded.players)

    database = migrated_connection()
    report = bootstrap_first_half(database, loaded)
    assert report["player_pool_count"] == 40
