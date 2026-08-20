"""Loads the coach-declared BBBFFL Grand Final team configuration.

This is a simple checked-in JSON file, not a database -- it represents the
coach-declared selection and is deliberately kept separate from scorer
decisions (which live in SQLite, see db.py). It is not mutated at runtime.

A roster slot's value may be `null` when the coach hasn't named a player
for it yet -- e.g. the Thursday-night Interchange loophole, where an early
Interchange pick is locked in while most of the starting lineup still
depends on Friday team announcements. `null` means "not yet named /
currently vacant"; it is a coach-declared-selection concept and is
distinct from DNP, which is a scorer decision about a player who *was*
named but didn't take the field (see db.py / service.py).
"""

import json
from dataclasses import dataclass
from functools import lru_cache

from app.scoring import ROSTER_SLOTS


@dataclass(frozen=True)
class TeamConfig:
    team_key: str
    name: str
    roster: dict[str, int | None]  # position/slot -> canonical_player_id, or None if not yet named


class TeamConfigError(ValueError):
    pass


def load_teams(path: str) -> list[TeamConfig]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    teams_raw = raw.get("teams")
    if not isinstance(teams_raw, list) or len(teams_raw) != 2:
        raise TeamConfigError("Team config must declare exactly two teams for the Grand Final.")

    teams = []
    seen_keys = set()
    for entry in teams_raw:
        team_key = entry.get("team_key")
        name = entry.get("name")
        roster = entry.get("roster")
        if not team_key or not name or not isinstance(roster, dict):
            raise TeamConfigError(f"Malformed team entry: {entry!r}")
        if team_key in seen_keys:
            raise TeamConfigError(f"Duplicate team_key: {team_key}")
        seen_keys.add(team_key)

        missing = [slot for slot in ROSTER_SLOTS if slot not in roster]
        if missing:
            raise TeamConfigError(f"Team '{team_key}' is missing roster slots: {missing}")
        extra = [slot for slot in roster if slot not in ROSTER_SLOTS]
        if extra:
            raise TeamConfigError(f"Team '{team_key}' has unknown roster slots: {extra}")

        teams.append(
            TeamConfig(
                team_key=team_key,
                name=name,
                # None -> not yet named (still validated "as before" otherwise:
                # any non-null value must be coercible to int).
                roster={
                    slot: (None if roster[slot] is None else int(roster[slot]))
                    for slot in ROSTER_SLOTS
                },
            )
        )
    return teams


@lru_cache(maxsize=1)
def _cached_load(path: str) -> tuple[TeamConfig, ...]:
    return tuple(load_teams(path))


def get_teams(path: str) -> list[TeamConfig]:
    """Cached accessor -- the declared team sheet does not change at runtime."""
    return list(_cached_load(path))
