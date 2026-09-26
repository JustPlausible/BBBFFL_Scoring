"""Shared fixtures for issue #237's season-setup tests: an in-memory stand-in
for the live afl-api client (the surface `app.season_setup` reads --
seasons, the season player list, rounds and matches, plus an
`evidence_batch` freshness scope) and a helper that builds a genuinely
fresh season with ten entries through the ordinary Season Centre
repositories. Never a live afl-api call."""

from contextlib import contextmanager

from app.afl_client import Match, Round, SeasonPlayerRecord, Team
from app.afl_client import Season as AflSeason
from app.audit import ActorContext
from app.identity import IdentityRepository
from app.season import SeasonRepository

SCORER = ActorContext("anonymous_operator", "scorer-coach-id", "scorer")

CLUBS = {1: "Adelaide", 2: "Brisbane Lions", 3: "Carlton", 4: "Collingwood", 5: "Essendon", 6: "Fremantle"}
OPENING_ROUND_ID = 500


def season_players(count=60, *, start=1000):
    return [
        SeasonPlayerRecord(start + n, f"Player {start + n}", Team((n % 6) + 1, CLUBS[(n % 6) + 1]))
        for n in range(count)
    ]


def opening_round_fixture(*, with_opening=True, unpublished_bye_round=None):
    """AFL rounds for a season with an Opening Round (round 0, clubs 1-4)
    whose compensating byes fall in AFL Rounds 2 (clubs 1, 2), 3 (club 3)
    and 4 (club 4)."""
    rounds = []
    if with_opening:
        rounds.append(Round(OPENING_ROUND_ID, 0, ()))
    byes = {1: (), 2: (Team(1, CLUBS[1]), Team(2, CLUBS[2])), 3: (Team(3, CLUBS[3]),), 4: (Team(4, CLUBS[4]),)}
    for number in range(1, 6):
        value = None if number == unpublished_bye_round else byes.get(number, ())
        rounds.append(Round(OPENING_ROUND_ID + number, number, value))
    matches = {
        OPENING_ROUND_ID: [
            Match(9001, Team(1, CLUBS[1]), Team(2, CLUBS[2]), "UPCOMING"),
            Match(9002, Team(3, CLUBS[3]), Team(4, CLUBS[4]), "UPCOMING"),
        ]
    }
    return rounds, matches


class _Evidence:
    def __init__(self, fresh):
        self._fresh = fresh

    def is_evidence_fresh(self):
        return self._fresh


class SetupAfl:
    """Duck-typed live afl-api stand-in for `app.season_setup`."""

    def __init__(self, *, year=2027, afl_season_id=77, players=None, rounds=None, matches=None, fresh=True):
        self.year = year
        self.afl_season_id = afl_season_id
        self.players = season_players() if players is None else players
        self.rounds, self.matches = (rounds, matches) if rounds is not None else opening_round_fixture()
        self.fresh = fresh
        self.season_player_calls = []

    def get_seasons(self):
        return [
            AflSeason(self.afl_season_id, True, 0, year=self.year, name=f"{self.year} AFL Premiership Season"),
            AflSeason(self.afl_season_id - 1, False, 24, year=self.year - 1, name=f"{self.year - 1} AFL Season"),
        ]

    def get_season_players(self, afl_season_id):
        self.season_player_calls.append(afl_season_id)
        return list(self.players)

    def get_rounds(self, afl_season_id):
        return list(self.rounds)

    def get_matches(self, round_id):
        return list(self.matches.get(round_id, []))

    @contextmanager
    def evidence_batch(self):
        yield _Evidence(self.fresh)


def fresh_season(database, *, year=2027, entry_count=10, regular_season_round_count=20):
    """A brand-new season in `setup` with `entry_count` coaches/teams, made
    through the same repositories the Season Centre routes call."""
    season = SeasonRepository(database).create_season(
        year, f"{year} BBBFFL Season", regular_season_round_count=regular_season_round_count
    )
    identities = IdentityRepository(database)
    entries = []
    for number in range(1, entry_count + 1):
        coach = identities.create_coach(f"Coach {year}-{number}")
        entries.append(
            identities.create_entry(season.season_id, f"entry-{year}-{number}", coach.coach_id, f"Team {number}")
        )
    return season, entries


def table_counts(database, *tables):
    return {table: database.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] for table in tables}


STRUCTURE_TABLES = (
    "season_rules_version",
    "competition_stream",
    "bbbffl_round",
    "season_player_pool",
    "season_squad_configuration",
    "season_draft",
    "draft_pick",
    "opening_round_rule",
    "finals_bracket",
    "superscore_stream",
    "audit_event",
)
