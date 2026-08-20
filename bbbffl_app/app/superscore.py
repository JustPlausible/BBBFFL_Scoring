"""SuperScore: an opt-in, all-in leaderboard competition that reuses the
same coach-declared-lineup schema and the same scoring engine as the Grand
Final (see teams.py / scoring.py / service.py).

A SuperScore competition instance is identified by (season, afl_round) and
ranks an arbitrary number of independent BBBFFL entries by total score --
it is not arranged into head-to-head matches, so it does not reuse
MatchupResult's two-team leader/margin fields.

Like teams.py, this is a simple checked-in JSON file, not a database --
the coach-declared SuperScore lineups are deliberately kept separate from
scorer decisions (which live in SQLite, see db.py). It is not mutated at
runtime.
"""

import json
from dataclasses import dataclass
from functools import lru_cache

from app.teams import TeamConfig, TeamConfigError, parse_roster


@dataclass(frozen=True)
class SuperScoreConfig:
    season: int
    afl_round: int
    entries: list[TeamConfig]


def competition_key(season: int, afl_round: int) -> str:
    """The DecisionsRepository scoping key for one SuperScore round. Keying
    by season+round (rather than a fixed constant, the way Grand Final uses
    "grand_final") keeps each round's scorer decisions and finalised result
    independently addressable and retained -- the foundation for the
    historical "four SuperScore rounds per season" reporting expected
    later, without needing a separate SuperScore results table now."""
    return f"superscore:{season}:{afl_round}"


def load_superscore_config(path: str) -> SuperScoreConfig:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    competition_type = raw.get("competition_type")
    if competition_type != "SUPERSCORE":
        raise TeamConfigError(
            f"SuperScore config must declare competition_type 'SUPERSCORE', got {competition_type!r}"
        )

    season = raw.get("season")
    afl_round = raw.get("afl_round")
    if not isinstance(season, int) or not isinstance(afl_round, int):
        raise TeamConfigError("SuperScore config must declare integer 'season' and 'afl_round'")

    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise TeamConfigError("SuperScore config must declare a non-empty 'entries' list")

    entries = []
    seen_keys = set()
    for entry in entries_raw:
        team_key = entry.get("team_key")
        coach = entry.get("coach")
        lineup = entry.get("lineup")
        if not team_key or not coach or not isinstance(lineup, dict):
            raise TeamConfigError(f"Malformed SuperScore entry: {entry!r}")
        if team_key in seen_keys:
            raise TeamConfigError(f"Duplicate team_key: {team_key}")
        seen_keys.add(team_key)

        entries.append(
            TeamConfig(
                team_key=team_key,
                name=coach,
                roster=parse_roster(lineup, f"SuperScore entry '{team_key}'"),
            )
        )

    return SuperScoreConfig(season=season, afl_round=afl_round, entries=entries)


@lru_cache(maxsize=1)
def _cached_load(path: str) -> SuperScoreConfig:
    return load_superscore_config(path)


def get_superscore_config(path: str) -> SuperScoreConfig:
    """Cached accessor -- the declared entry sheet does not change at runtime."""
    return _cached_load(path)
