"""Hermetic AFL evidence and reporting primitives for deterministic replay.

Replay replaces only the AFL boundary and effective clock.  Consumers receive
the same dataclasses as :class:`app.afl_client.AflApiClient`; competition
commands therefore continue through the normal lineup, calculation, review,
official-result and ladder services.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from app.afl_client import Match, Player, PlayerStatLine, Round, Season, Team


class ReplayEvidenceError(ValueError):
    """Controlled evidence is absent, malformed, or internally inconsistent."""


class EvidenceClass(str, Enum):
    KNOWN_FACT = "known_fact"
    RECONSTRUCTABLE_BEHAVIOUR = "reconstructable_behaviour"
    SYNTHETIC_SCENARIO = "synthetic_scenario"
    UNRESOLVED_SCORER_INPUT = "unresolved_scorer_input"


@dataclass(frozen=True)
class ReplayClock:
    """Explicit effective time; deliberately has no wall-clock fallback."""

    effective_at: datetime

    def __post_init__(self) -> None:
        if self.effective_at.tzinfo is None:
            raise ValueError("replay effective time must be timezone-aware")

    @classmethod
    def from_iso(cls, value: str) -> "ReplayClock":
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReplayEvidenceError(f"invalid replay effective timestamp {value!r}") from exc
        return cls(parsed.astimezone(timezone.utc))

    def now(self) -> datetime:
        return self.effective_at


class ReplayAflDataSource:
    """Strict, in-memory implementation of the production AFL data seam.

    Loading is eager and validates the complete manifest.  There is no live
    client member and no fallback branch, making network access impossible by
    construction once replay mode has been selected.
    """

    SCHEMA = "bbbffl.replay-evidence/v1"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise ReplayEvidenceError(f"replay evidence file does not exist: {self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReplayEvidenceError(f"malformed replay evidence at {self.path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ReplayEvidenceError("replay evidence root must be an object")
        self._load(payload)

    @staticmethod
    def _list(payload: dict, key: str) -> list[dict]:
        value = payload.get(key)
        if not isinstance(value, list):
            raise ReplayEvidenceError(f"replay evidence field {key!r} must be a list")
        if not all(isinstance(item, dict) for item in value):
            raise ReplayEvidenceError(f"replay evidence field {key!r} must contain objects")
        return value

    def _load(self, payload: dict) -> None:
        if payload.get("schema") != self.SCHEMA:
            raise ReplayEvidenceError(f"unsupported replay evidence schema: {payload.get('schema')!r}")
        manifest = payload.get("manifest")
        if not isinstance(manifest, dict) or not manifest.get("id") or not manifest.get("version"):
            raise ReplayEvidenceError("manifest.id and manifest.version are required")
        try:
            EvidenceClass(manifest["evidence_class"])
        except (KeyError, ValueError) as exc:
            raise ReplayEvidenceError("manifest.evidence_class is missing or unknown") from exc
        self.manifest = dict(manifest)
        try:
            self._seasons = {
                int(x["season_id"]): Season(int(x["season_id"]), bool(x.get("is_current", False)), x.get("current_round_number"), int(x["year"]))
                for x in self._list(payload, "seasons")
            }
            self._rounds = {
                int(x["round_id"]): Round(
                    int(x["round_id"]), int(x["round_number"]),
                    tuple(Team(int(t["team_id"]), str(t.get("name", ""))) for t in x.get("byes", []))
                    if x.get("byes") is not None else None,
                ) for x in self._list(payload, "rounds")
            }
            self._round_seasons = {int(x["round_id"]): int(x["season_id"]) for x in self._list(payload, "rounds")}
            self._matches = {
                int(x["match_id"]): Match(
                    int(x["match_id"]), Team(int(x["home_team"]["team_id"]), str(x["home_team"].get("name", ""))),
                    Team(int(x["away_team"]["team_id"]), str(x["away_team"].get("name", ""))),
                    str(x["status"]), x.get("start_time_utc"),
                ) for x in self._list(payload, "matches")
            }
            self._match_rounds = {int(x["match_id"]): int(x["round_id"]) for x in self._list(payload, "matches")}
            self._players = {
                int(x["canonical_player_id"]): Player(
                    int(x["canonical_player_id"]), str(x["display_name"]),
                    Team(int(x["team_id"]), str(x.get("team_name", ""))),
                ) for x in self._list(payload, "players")
            }
            self._stats = {
                int(match_id): {
                    int(x["canonical_player_id"]): PlayerStatLine(
                        int(x["canonical_player_id"]), **{k: x.get(k, 0) for k in ("goals", "behinds", "disposals", "marks", "hitouts", "tackles")}
                    ) for x in rows
                } for match_id, rows in payload.get("player_stats", {}).items()
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplayEvidenceError(f"malformed replay evidence record: {exc}") from exc
        if not self._seasons or not self._rounds or not self._matches:
            raise ReplayEvidenceError("replay evidence requires at least one season, round, and match")
        for round_id, season_id in self._round_seasons.items():
            if season_id not in self._seasons:
                raise ReplayEvidenceError(f"round {round_id} references missing season {season_id}")
        for match_id, round_id in self._match_rounds.items():
            if round_id not in self._rounds:
                raise ReplayEvidenceError(f"match {match_id} references missing round {round_id}")
            if match_id not in self._stats:
                raise ReplayEvidenceError(f"required player_stats evidence missing for match {match_id}")
        for match_id, rows in self._stats.items():
            if match_id not in self._matches:
                raise ReplayEvidenceError(f"player_stats references missing match {match_id}")
            unknown = set(rows) - set(self._players)
            if unknown:
                raise ReplayEvidenceError(f"match {match_id} stats reference missing players {sorted(unknown)}")

    def close(self) -> None:
        pass

    def get_current_season(self) -> Season:
        current = [season for season in self._seasons.values() if season.is_current]
        if len(current) != 1:
            raise ReplayEvidenceError(f"expected exactly one current season, found {len(current)}")
        return current[0]

    def get_round(self, season_id: int, round_number: int) -> Round:
        found = [r for rid, r in self._rounds.items() if self._round_seasons[rid] == season_id and r.round_number == round_number]
        if len(found) != 1:
            raise ReplayEvidenceError(f"required AFL round is missing or ambiguous: season={season_id}, round={round_number}")
        return found[0]

    def get_rounds(self, season_id: int) -> list[Round]:
        if season_id not in self._seasons:
            raise ReplayEvidenceError(f"required AFL season is missing: {season_id}")
        return sorted((r for rid, r in self._rounds.items() if self._round_seasons[rid] == season_id), key=lambda r: r.round_number)

    def get_matches(self, round_id: int) -> list[Match]:
        if round_id not in self._rounds:
            raise ReplayEvidenceError(f"required AFL round evidence is missing: {round_id}")
        return sorted((m for mid, m in self._matches.items() if self._match_rounds[mid] == round_id), key=lambda m: m.match_id)

    def get_player(self, canonical_player_id: int) -> Player:
        try:
            return self._players[canonical_player_id]
        except KeyError as exc:
            raise ReplayEvidenceError(f"required player identity is missing: {canonical_player_id}") from exc

    def get_match_player_stats(self, match_id: int) -> dict[int, PlayerStatLine]:
        try:
            return dict(self._stats[match_id])
        except KeyError as exc:
            raise ReplayEvidenceError(f"required player-stat evidence is missing: match={match_id}") from exc


def write_replay_report(report: dict[str, Any], json_path: str | Path, summary_path: str | Path) -> None:
    """Write stable JSON plus a concise operator-readable summary."""
    required = ("run", "mapping", "lineups", "lockout", "scoring", "scorer_workflow", "official_results", "ladder", "discrepancies")
    missing = [key for key in required if key not in report]
    if missing:
        raise ReplayEvidenceError(f"report is incomplete; missing sections: {', '.join(missing)}")
    Path(json_path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    run = report["run"]
    lines = [
        f"BBBFFL replay {run['run_id']}",
        f"Season {run['season']} round {run['round']} | evidence {run['evidence_manifest']}@{run['evidence_version']}",
        f"Lineups: {len(report['lineups'])} | Matchups: {len(report['scoring'])} | Official results: {len(report['official_results'])}",
        f"Discrepancies: {len(report['discrepancies'])}",
    ]
    Path(summary_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
