"""Proves AflApiClient parses the CONFIRMED live afl-api /api/v1 contracts
(from live integration testing), not the earlier inferred shapes. Payloads
below are the verbatim confirmed response shapes, trimmed only of fields the
adapter doesn't consume.

Uses httpx.MockTransport so these run offline against fixed JSON, while
still exercising the real AflApiClient parsing code end-to-end.
"""

import httpx
import pytest

from app.afl_client import (
    AflApiClient,
    Match,
    Player,
    PlayerStatLine,
    Round,
    Season,
    Team,
    normalize_match_status,
)

SEASONS_PAYLOAD = {
    "seasons": [
        {
            "season_id": 85,
            "year": 2026,
            "name": "2026 Toyota AFL Premiership",
            "is_current": True,
            "current_round_number": 24,
        }
    ]
}

ROUNDS_PAYLOAD = {
    "rounds": [
        {
            "round_id": 1367,
            "season_id": 85,
            "round_number": 24,
            "name": "Round 24",
            "abbreviation": "Rd 24",
            "start_time": "2026-09-25T09:00:00Z",
            "end_time": "2026-09-27T09:00:00Z",
            "byes": [],
        }
    ]
}

MATCHES_PAYLOAD = {
    "matches": [
        {
            "match_id": 8242,
            "round_id": 1367,
            "season_id": 85,
            "status": "LIVE",
            "start_time_utc": "2026-09-26T08:40:00Z",
            "home_team": {"team_id": 11, "name": "St Kilda"},
            "away_team": {"team_id": 4, "name": "Gold Coast SUNS"},
            "score_home": None,
            "score_away": None,
        }
    ]
}

PLAYER_PAYLOAD = {
    "player": {
        "canonical_player_id": 396,
        "display_name": "Josh Daicos",
        "current_team": {"team_id": 3, "name": "Collingwood"},
        "identifiers": {"afl_player_id": 1321, "champion_data_player_id": "CD_I1005054"},
    }
}

PLAYER_STATS_PAYLOAD = {
    "match": {"match_id": 8242, "round_id": 1367, "season_id": 85, "status": "LIVE"},
    "lifecycle": {"finality": "not_available"},
    "metadata": {"source_updated_at": "2026-09-26T09:10:00Z"},
    "players": [
        {
            "canonical_player_id": 384,
            "display_name": "John Noble",
            "team_id": 4,
            "stats": {
                "goals": 0,
                "behinds": 0,
                "disposals": 24,
                "marks": 5,
                "tackles": 0,
                "hitouts": 0,
            },
        }
    ],
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    routes = {
        "/api/v1/seasons": SEASONS_PAYLOAD,
        "/api/v1/seasons/85/rounds": ROUNDS_PAYLOAD,
        "/api/v1/rounds/1367/matches": MATCHES_PAYLOAD,
        "/api/v1/players/396": PLAYER_PAYLOAD,
        "/api/v1/matches/8242/player-stats": PLAYER_STATS_PAYLOAD,
    }
    if path in routes:
        return httpx.Response(200, json=routes[path])
    return httpx.Response(404, json={"detail": f"no mock route for {path}"})


@pytest.fixture
def client():
    api = AflApiClient(base_url="http://afl-api.test")
    # Swap in a mock transport so this exercises AflApiClient's real request
    # + parsing code with no network involved.
    api._client = httpx.Client(base_url="http://afl-api.test", transport=httpx.MockTransport(_handler))
    yield api
    api.close()


def test_get_current_season_reads_seasons_wrapper_and_season_id(client):
    season = client.get_current_season()
    assert season == Season(season_id=85, is_current=True, current_round_number=24, year=2026)


def test_get_round_reads_rounds_wrapper_and_round_id(client):
    round_ = client.get_round(85, 24)
    assert round_ == Round(round_id=1367, round_number=24)


def test_get_matches_reads_matches_wrapper_and_nested_team_objects(client):
    matches = client.get_matches(1367)
    assert matches == [
        Match(
            match_id=8242,
            home_team=Team(team_id=11, name="St Kilda"),
            away_team=Team(team_id=4, name="Gold Coast SUNS"),
            status="LIVE",
        )
    ]
    assert matches[0].state == "live"
    assert matches[0].involves_team(4)
    assert matches[0].involves_team(11)
    assert not matches[0].involves_team(999)


def test_get_player_unwraps_player_key_and_uses_display_name(client):
    player = client.get_player(396)
    assert player == Player(
        canonical_player_id=396,
        name="Josh Daicos",
        current_team=Team(team_id=3, name="Collingwood"),
    )


@pytest.mark.parametrize(
    "raw_status,expected",
    [
        # Canonical afl-api v1 lifecycle values (AFL-api/docs/api_v1_matches.md).
        ("UPCOMING", "yet_to_play"),
        ("LIVE", "live"),
        ("CONCLUDED", "completed"),
        # Case-insensitivity on the canonical values.
        ("concluded", "completed"),
        ("live", "live"),
        # Legacy/inferred aliases retained for backwards compatibility.
        ("FINAL", "completed"),
        ("FT", "completed"),
        ("FULL_TIME", "completed"),
        ("COMPLETE", "completed"),
        ("COMPLETED", "completed"),
        ("IN_PROGRESS", "live"),
        ("IN PROGRESS", "live"),
        ("SCHEDULED", "yet_to_play"),
        ("NOT_STARTED", "yet_to_play"),
        # Unrecognised/empty values fall back to yet_to_play, never completed.
        ("", "yet_to_play"),
        ("SOME_UNKNOWN_STATUS", "yet_to_play"),
    ],
)
def test_normalize_match_status_maps_afl_api_v1_lifecycle_values(raw_status, expected):
    assert normalize_match_status(raw_status) == expected


def test_match_state_property_delegates_to_normalize_match_status():
    concluded = Match(
        match_id=1, home_team=Team(1, "Cats"), away_team=Team(2, "Pies"), status="CONCLUDED"
    )
    assert concluded.state == "completed"


def test_get_match_player_stats_reads_players_wrapper_and_nested_stats(client):
    stats = client.get_match_player_stats(8242)
    assert stats == {
        384: PlayerStatLine(
            canonical_player_id=384,
            goals=0,
            behinds=0,
            disposals=24,
            marks=5,
            hitouts=0,
            tackles=0,
        )
    }
