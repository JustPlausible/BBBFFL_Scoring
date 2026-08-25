
import pytest

from tests.db_helpers import migrated_connection

from app.afl_client import Match, Player, PlayerStatLine, Team
from app.db import DecisionsRepository
from app.teams import TeamConfig

CATS = Team(team_id=1001, name="Cats")
PIES = Team(team_id=1002, name="Pies")


class FakeSeason:
    def __init__(self, season_id=1, round_number=1, year=2026):
        self.season_id = season_id
        self.is_current = True
        self.current_round_number = round_number
        self.year = year


class FakeRound:
    def __init__(self, round_id=1, round_number=1):
        self.round_id = round_id
        self.round_number = round_number


class FakeAflClient:
    """A duck-typed stand-in for AflApiClient -- no network involved."""

    def __init__(
        self, matches, players, stats_by_match=None, season_id=1, current_round_number=1, year=2026
    ):
        self.matches = matches
        self.players = players
        self.stats_by_match = stats_by_match or {}
        self.stats_fetch_calls = []
        self.get_player_calls = []
        self.get_round_calls = []
        self._season = FakeSeason(season_id=season_id, round_number=current_round_number, year=year)

    def get_current_season(self):
        return self._season

    def get_round(self, season_id, round_number):
        self.get_round_calls.append((season_id, round_number))
        return FakeRound(round_number=round_number)

    def get_matches(self, round_id):
        return self.matches

    def get_player(self, canonical_player_id):
        self.get_player_calls.append(canonical_player_id)
        return self.players[canonical_player_id]

    def get_match_player_stats(self, match_id):
        self.stats_fetch_calls.append(match_id)
        return self.stats_by_match.get(match_id, {})


@pytest.fixture
def decisions():
    conn = migrated_connection()
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
def partial_teams():
    """team_a mirrors the Thursday-night Interchange loophole: only the
    Interchange is named, the rest of the lineup awaits Friday team news.
    team_b is fully named, so tests can check named/unnamed coexist fine."""
    partial_roster = dict.fromkeys(TEAM_A_ROSTER)
    partial_roster["Interchange"] = TEAM_A_ROSTER["Interchange"]
    return [
        TeamConfig(team_key="team_a", name="Alpha", roster=partial_roster),
        TeamConfig(team_key="team_b", name="Bravo", roster=dict(TEAM_B_ROSTER)),
    ]


@pytest.fixture
def single_match():
    return Match(match_id=100, home_team=CATS, away_team=PIES, status="LIVE")


@pytest.fixture
def players_on_one_match():
    """All 18 rostered players resolved to one of the two clubs in `single_match`."""
    players = {}
    for slot_ids, team in ((TEAM_A_ROSTER.values(), CATS), (TEAM_B_ROSTER.values(), PIES)):
        for pid in slot_ids:
            players[pid] = Player(canonical_player_id=pid, name=f"Player {pid}", current_team=team)
    return players


def stat_line(player_id, **kwargs):
    return PlayerStatLine(canonical_player_id=player_id, **kwargs)
