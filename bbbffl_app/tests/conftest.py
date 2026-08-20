import sqlite3

import pytest

from app.afl_client import Match, Player, PlayerStatLine
from app.db import DecisionsRepository, init_db
from app.teams import TeamConfig


class FakeSeason:
    def __init__(self, season_id=1, round_number=1):
        self.id = season_id
        self.is_current = True
        self.current_round_number = round_number


class FakeRound:
    def __init__(self, round_id=1, round_number=1):
        self.id = round_id
        self.round_number = round_number


class FakeAflClient:
    """A duck-typed stand-in for AflApiClient -- no network involved."""

    def __init__(self, matches, players, stats_by_match=None):
        self.matches = matches
        self.players = players
        self.stats_by_match = stats_by_match or {}
        self.stats_fetch_calls = []

    def get_current_season(self):
        return FakeSeason()

    def get_round(self, season_id, round_number):
        return FakeRound(round_number=round_number)

    def get_matches(self, round_id):
        return self.matches

    def get_player(self, canonical_player_id):
        return self.players[canonical_player_id]

    def get_match_player_stats(self, match_id):
        self.stats_fetch_calls.append(match_id)
        return self.stats_by_match.get(match_id, {})


@pytest.fixture
def decisions():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return DecisionsRepository(conn)


TEAM_A_ROSTER = {
    "Forward1": 1,
    "Forward2": 2,
    "Forward3": 3,
    "Midfield1": 4,
    "Midfield2": 5,
    "Midfield3": 6,
    "Ruck": 7,
    "Tackler": 8,
    "Interchange": 9,
}

TEAM_B_ROSTER = {
    "Forward1": 11,
    "Forward2": 12,
    "Forward3": 13,
    "Midfield1": 14,
    "Midfield2": 15,
    "Midfield3": 16,
    "Ruck": 17,
    "Tackler": 18,
    "Interchange": 19,
}


@pytest.fixture
def teams():
    return [
        TeamConfig(team_key="team_a", name="Alpha", roster=dict(TEAM_A_ROSTER)),
        TeamConfig(team_key="team_b", name="Bravo", roster=dict(TEAM_B_ROSTER)),
    ]


@pytest.fixture
def single_match():
    return Match(id=100, home_team="Cats", away_team="Pies", status="LIVE")


@pytest.fixture
def players_on_one_match():
    """All 18 rostered players resolved to one of the two clubs in `single_match`."""
    players = {}
    for slot_ids, club in ((TEAM_A_ROSTER.values(), "Cats"), (TEAM_B_ROSTER.values(), "Pies")):
        for pid in slot_ids:
            players[pid] = Player(canonical_player_id=pid, name=f"Player {pid}", current_team=club)
    return players


def stat_line(player_id, **kwargs):
    return PlayerStatLine(canonical_player_id=player_id, **kwargs)
