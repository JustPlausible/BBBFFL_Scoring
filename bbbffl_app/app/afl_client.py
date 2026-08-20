"""Adapter for the `afl-api` /api/v1 consumer API.

afl-api is authoritative for AFL data; BBBFFL must not reproduce AFL
collection or maintain a second authoritative copy of AFL statistics. This
module is the single seam that turns afl-api's raw JSON into the internal
dataclasses the rest of the app uses -- if a field name below turns out to
differ from the real deployed API, this is the only file that needs to
change.

ASSUMPTION (unconfirmed against a live afl-api instance at the time this was
written -- see docs/plans/2027-grand-final-prototype-brief.md and the
implementation notes in bbbffl_app/README.md):
  - GET /api/v1/seasons returns a list of season objects, each with
    `id`, `is_current` (bool) and, on the current season, `current_round_number`.
  - GET /api/v1/seasons/{season_id}/rounds returns a list of round objects
    with `id` and `round_number`.
  - GET /api/v1/rounds/{round_id}/matches returns a list of match objects
    with `id`, `home_team`, `away_team` and a `status` string.
  - GET /api/v1/players/{canonical_player_id} returns an object with
    `canonical_player_id`, `name` and `current_team`.
  - GET /api/v1/matches/{match_id}/player-stats returns a list of per-player
    stat lines, each identified by `canonical_player_id` and carrying
    `goals`, `behinds`, `disposals`, `marks`, `hitouts`, `tackles`.
  - Match completion is signalled by `status`, normalised case-insensitively
    below. A prototype run against the live API may need to extend
    `normalize_match_status` with additional raw values observed in
    practice.
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


@dataclass(frozen=True)
class Season:
    id: int
    is_current: bool
    current_round_number: int | None


@dataclass(frozen=True)
class Round:
    id: int
    round_number: int


@dataclass(frozen=True)
class Match:
    id: int
    home_team: str
    away_team: str
    status: str

    @property
    def state(self) -> MatchState:
        return normalize_match_status(self.status)

    def involves_team(self, team: str) -> bool:
        return team in (self.home_team, self.away_team)


@dataclass(frozen=True)
class Player:
    canonical_player_id: int
    name: str
    current_team: str


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
        seasons = payload if isinstance(payload, list) else payload.get("results", payload)
        for entry in seasons:
            if entry.get("is_current"):
                return Season(
                    id=entry["id"],
                    is_current=True,
                    current_round_number=entry.get("current_round_number"),
                )
        raise AflApiError("afl-api /api/v1/seasons returned no season with is_current=true")

    def get_round(self, season_id: int, round_number: int) -> Round:
        payload = self._get(f"/api/v1/seasons/{season_id}/rounds")
        rounds = payload if isinstance(payload, list) else payload.get("results", payload)
        for entry in rounds:
            if entry.get("round_number") == round_number:
                return Round(id=entry["id"], round_number=round_number)
        raise AflApiError(
            f"afl-api returned no round {round_number} for season {season_id}"
        )

    def get_matches(self, round_id: int) -> list[Match]:
        payload = self._get(f"/api/v1/rounds/{round_id}/matches")
        matches = payload if isinstance(payload, list) else payload.get("results", payload)
        return [
            Match(
                id=entry["id"],
                home_team=entry.get("home_team", ""),
                away_team=entry.get("away_team", ""),
                status=entry.get("status", ""),
            )
            for entry in matches
        ]

    def get_player(self, canonical_player_id: int) -> Player:
        entry = self._get(f"/api/v1/players/{canonical_player_id}")
        return Player(
            canonical_player_id=entry["canonical_player_id"],
            name=entry.get("name", f"Player {canonical_player_id}"),
            current_team=entry.get("current_team", ""),
        )

    def get_match_player_stats(self, match_id: int) -> dict[int, PlayerStatLine]:
        payload = self._get(f"/api/v1/matches/{match_id}/player-stats")
        rows = payload if isinstance(payload, list) else payload.get("results", payload)
        stats: dict[int, PlayerStatLine] = {}
        for row in rows:
            player_id = row.get("canonical_player_id")
            if player_id is None:
                continue
            stats[player_id] = PlayerStatLine(
                canonical_player_id=player_id,
                goals=int(row.get("goals") or 0),
                behinds=int(row.get("behinds") or 0),
                disposals=int(row.get("disposals") or 0),
                marks=int(row.get("marks") or 0),
                hitouts=int(row.get("hitouts") or 0),
                tackles=int(row.get("tackles") or 0),
            )
        return stats
