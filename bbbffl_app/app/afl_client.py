"""Adapter for the `afl-api` /api/v1 consumer API.

afl-api is authoritative for AFL data; BBBFFL must not reproduce AFL
collection or maintain a second authoritative copy of AFL statistics. This
module is the single seam that turns afl-api's raw JSON into the internal
dataclasses the rest of the app uses -- if a field name here ever drifts
from the deployed API, this is the only file that needs to change.

CONFIRMED live contracts (from live integration testing against a deployed
afl-api instance, superseding the earlier inferred assumptions):

  GET /api/v1/seasons
    {"seasons": [{"season_id": int, "year": int, "name": str,
                  "is_current": bool, "current_round_number": int}]}

  GET /api/v1/seasons/{season_id}/rounds
    {"rounds": [{"round_id": int, "season_id": int, "round_number": int,
                 "name": str, "abbreviation": str, "start_time": str,
                 "end_time": str, "byes": [...]}]}

  GET /api/v1/rounds/{round_id}/matches
    {"matches": [{"match_id": int, "round_id": int, "season_id": int,
                  "status": str, "start_time_utc": str,
                  "home_team": {"team_id": int, "name": str},
                  "away_team": {"team_id": int, "name": str},
                  "score_home": int | null, "score_away": int | null}]}

  GET /api/v1/players/{canonical_player_id}
    {"player": {"canonical_player_id": int, "display_name": str,
                "current_team": {"team_id": int, "name": str},
                "identifiers": {...}}}

  GET /api/v1/matches/{match_id}/player-stats
    {"match": {...}, "lifecycle": {"finality": str}, "metadata": {...},
     "players": [{"canonical_player_id": int, "display_name": str,
                  "team_id": int,
                  "stats": {"goals": int, "behinds": int, "disposals": int,
                            "marks": int, "tackles": int, "hitouts": int}}]}

`canonical_player_id` remains BBBFFL's stored AFL player identity
throughout. Team identity (for matching a rostered player to their AFL
match) is carried as a `team_id`, not a name -- names are for display only
and are not guaranteed stable/unique the way `team_id` is.
"""

import logging
from dataclasses import dataclass
from typing import Literal

import httpx

logger = logging.getLogger("bbbffl.afl_client")

MatchState = Literal["yet_to_play", "live", "completed"]

_COMPLETED_STATUSES = {"FINAL", "FT", "FULL_TIME", "COMPLETE", "COMPLETED"}
_LIVE_STATUSES = {"LIVE", "IN_PROGRESS", "IN PROGRESS"}


def normalize_match_status(raw_status: str) -> MatchState:
    normalized = (raw_status or "").strip().upper()
    if normalized in _COMPLETED_STATUSES:
        return "completed"
    if normalized in _LIVE_STATUSES:
        return "live"
    return "yet_to_play"


def _unwrap(payload: dict | list, key: str) -> list:
    """afl-api wraps list responses in a named key (e.g. {"seasons": [...]}).
    Falls back to a bare list, or the older "results" wrapper, for
    resilience against minor response-shape variation."""
    if isinstance(payload, list):
        return payload
    if key in payload:
        return payload[key]
    return payload.get("results", payload)


@dataclass(frozen=True)
class Team:
    team_id: int
    name: str

    @staticmethod
    def from_json(entry: dict) -> "Team":
        return Team(team_id=entry["team_id"], name=entry.get("name", ""))


@dataclass(frozen=True)
class Season:
    season_id: int
    is_current: bool
    current_round_number: int | None
    # Populated from afl-api's "year" field. Lets a caller that has a
    # season/round declared elsewhere (e.g. a SuperScore config) detect a
    # stale configuration -- e.g. still declaring last year's season after
    # year rollover -- before scoring against the wrong season's data.
    year: int | None = None


@dataclass(frozen=True)
class Round:
    round_id: int
    round_number: int


@dataclass(frozen=True)
class Match:
    match_id: int
    home_team: Team
    away_team: Team
    status: str

    @property
    def state(self) -> MatchState:
        return normalize_match_status(self.status)

    def involves_team(self, team_id: int) -> bool:
        return team_id in (self.home_team.team_id, self.away_team.team_id)


@dataclass(frozen=True)
class Player:
    canonical_player_id: int
    name: str
    current_team: Team


@dataclass(frozen=True)
class PlayerStatLine:
    canonical_player_id: int
    goals: int = 0
    behinds: int = 0
    disposals: int = 0
    marks: int = 0
    hitouts: int = 0
    tackles: int = 0


class AflApiError(RuntimeError):
    pass


class AflApiClient:
    """Thin sync client for afl-api /api/v1. One instance per app process."""

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 10.0):
        headers = {"x-api-key": api_key} if api_key else {}
        self._client = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("afl-api request failed: GET %s (%s)", path, exc)
            raise AflApiError(f"afl-api request failed: GET {path} ({exc})") from exc
        return response.json()

    def get_current_season(self) -> Season:
        payload = self._get("/api/v1/seasons")
        for entry in _unwrap(payload, "seasons"):
            if entry.get("is_current"):
                return Season(
                    season_id=entry["season_id"],
                    is_current=True,
                    current_round_number=entry.get("current_round_number"),
                    year=entry.get("year"),
                )
        raise AflApiError("afl-api /api/v1/seasons returned no season with is_current=true")

    def get_round(self, season_id: int, round_number: int) -> Round:
        payload = self._get(f"/api/v1/seasons/{season_id}/rounds")
        for entry in _unwrap(payload, "rounds"):
            if entry.get("round_number") == round_number:
                return Round(round_id=entry["round_id"], round_number=round_number)
        raise AflApiError(
            f"afl-api returned no round {round_number} for season {season_id}"
        )

    def get_matches(self, round_id: int) -> list[Match]:
        payload = self._get(f"/api/v1/rounds/{round_id}/matches")
        return [
            Match(
                match_id=entry["match_id"],
                home_team=Team.from_json(entry["home_team"]),
                away_team=Team.from_json(entry["away_team"]),
                status=entry.get("status", ""),
            )
            for entry in _unwrap(payload, "matches")
        ]

    def get_player(self, canonical_player_id: int) -> Player:
        payload = self._get(f"/api/v1/players/{canonical_player_id}")
        entry = payload["player"] if isinstance(payload, dict) and "player" in payload else payload
        return Player(
            canonical_player_id=entry["canonical_player_id"],
            name=entry.get("display_name", f"Player {canonical_player_id}"),
            current_team=Team.from_json(entry.get("current_team", {"team_id": 0, "name": ""})),
        )

    def get_match_player_stats(self, match_id: int) -> dict[int, PlayerStatLine]:
        payload = self._get(f"/api/v1/matches/{match_id}/player-stats")
        stats: dict[int, PlayerStatLine] = {}
        for row in _unwrap(payload, "players"):
            player_id = row.get("canonical_player_id")
            if player_id is None:
                continue
            row_stats = row.get("stats", {})
            stats[player_id] = PlayerStatLine(
                canonical_player_id=player_id,
                goals=int(row_stats.get("goals") or 0),
                behinds=int(row_stats.get("behinds") or 0),
                disposals=int(row_stats.get("disposals") or 0),
                marks=int(row_stats.get("marks") or 0),
                hitouts=int(row_stats.get("hitouts") or 0),
                tackles=int(row_stats.get("tackles") or 0),
            )
        return stats
