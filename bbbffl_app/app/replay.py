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
from app.ladder import LadderRepository


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

    def __init__(
        self,
        path: str | Path,
        *,
        clock: ReplayClock | None = None,
        checkpoint_path: str | Path | None = None,
    ):
        self.path = Path(path)
        self.clock = clock
        if not self.path.is_file():
            raise ReplayEvidenceError(f"replay evidence file does not exist: {self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReplayEvidenceError(f"malformed replay evidence at {self.path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ReplayEvidenceError("replay evidence root must be an object")
        self.stage: str | None = None
        if checkpoint_path is not None:
            self._load_checkpoint(Path(checkpoint_path))
        self._load(payload)

    def _load_checkpoint(self, path: Path) -> None:
        if not path.is_file():
            raise ReplayEvidenceError(f"replay checkpoint file does not exist: {path}")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("schema") != "bbbffl.replay-checkpoint/v1":
                raise ReplayEvidenceError(f"unsupported replay checkpoint schema: {state.get('schema')!r}")
            if state.get("stage") not in ("scheduled", "final-results"):
                raise ReplayEvidenceError(f"unsupported replay checkpoint stage: {state.get('stage')!r}")
            self.clock = ReplayClock.from_iso(state["effective_at"])
            self.stage = state["stage"]
        except ReplayEvidenceError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ReplayEvidenceError(f"malformed replay checkpoint at {path}: {exc}") from exc

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
        historical = manifest.get("lifecycle_semantics") == "scheduled-start-plus-final-results-checkpoint"
        if historical and (self.clock is None or self.stage is None):
            raise ReplayEvidenceError("historical replay package requires an explicit persisted replay checkpoint")
        # A replay may carry its own explicit effective instant so an
        # interactive application process uses the same deterministic clock
        # as a repository-level replay. A caller-supplied clock remains the
        # highest-precedence choice. Ordinary/live AFL sources never pass
        # through this boundary and manifests without the field retain the
        # existing behaviour.
        if self.clock is None and manifest.get("replay_effective_at") is not None:
            self.clock = ReplayClock.from_iso(manifest["replay_effective_at"])
        self._payload = payload
        for section in ("seasons", "rounds", "matches", "players"):
            for index, record in enumerate(self._list(payload, section)):
                self._validate_provenance(record, f"{section}[{index}]")
        for match_id, records in payload.get("player_stats", {}).items():
            if not isinstance(records, list):
                raise ReplayEvidenceError(f"player_stats[{match_id!r}] must be a list")
            for index, record in enumerate(records):
                self._validate_provenance(record, f"player_stats[{match_id!r}][{index}]")
        for index, record in enumerate(self._list(payload, "lineups")):
            self._validate_provenance(record, f"lineups[{index}]")
        try:
            self._seasons = {
                int(x["season_id"]): Season(
                    int(x["season_id"]), bool(x.get("is_current", False)), x.get("current_round_number"), int(x["year"])
                )
                for x in self._list(payload, "seasons")
            }
            self._rounds = {
                int(x["round_id"]): Round(
                    int(x["round_id"]),
                    int(x["round_number"]),
                    tuple(Team(int(t["team_id"]), str(t.get("name", ""))) for t in x.get("byes", []))
                    if x.get("byes") is not None
                    else None,
                )
                for x in self._list(payload, "rounds")
            }
            self._round_seasons = {int(x["round_id"]): int(x["season_id"]) for x in self._list(payload, "rounds")}
            self._matches = {
                int(x["match_id"]): Match(
                    int(x["match_id"]),
                    Team(int(x["home_team"]["team_id"]), str(x["home_team"].get("name", ""))),
                    Team(int(x["away_team"]["team_id"]), str(x["away_team"].get("name", ""))),
                    str(x["status"]),
                    x.get("start_time_utc"),
                )
                for x in self._list(payload, "matches")
            }
            self._match_records = {int(x["match_id"]): x for x in self._list(payload, "matches")}
            self._match_rounds = {int(x["match_id"]): int(x["round_id"]) for x in self._list(payload, "matches")}
            self._players = {
                int(x["canonical_player_id"]): Player(
                    int(x["canonical_player_id"]),
                    str(x["display_name"]),
                    Team(int(x["team_id"]), str(x.get("team_name", ""))),
                )
                for x in self._list(payload, "players")
            }
            self._stats = {
                int(match_id): {
                    int(x["canonical_player_id"]): PlayerStatLine(
                        int(x["canonical_player_id"]),
                        **{k: x.get(k, 0) for k in ("goals", "behinds", "disposals", "marks", "hitouts", "tackles")},
                    )
                    for x in rows
                }
                for match_id, rows in payload.get("player_stats", {}).items()
            }
            self.lineup_inputs = tuple(dict(x) for x in self._list(payload, "lineups"))
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
        historical_entries = [lineup.get("historical_entry") for lineup in self.lineup_inputs]
        if len(historical_entries) != len(set(historical_entries)):
            raise ReplayEvidenceError("replay lineups must have unique historical_entry identities")
        for lineup in self.lineup_inputs:
            positions = lineup.get("positions")
            if not isinstance(positions, dict) or not positions:
                raise ReplayEvidenceError(
                    f"lineup {lineup.get('historical_entry')!r} requires a non-empty positions mapping"
                )
            unknown = {int(player_id) for player_id in positions.values()} - set(self._players)
            if unknown:
                raise ReplayEvidenceError(
                    f"lineup {lineup.get('historical_entry')!r} references missing players {sorted(unknown)}"
                )

    @staticmethod
    def _validate_provenance(record: dict, location: str) -> None:
        provenance = record.get("provenance")
        if not isinstance(provenance, dict) or not provenance.get("source"):
            raise ReplayEvidenceError(f"{location}.provenance.source is required")
        try:
            EvidenceClass(provenance["evidence_class"])
        except (KeyError, ValueError) as exc:
            raise ReplayEvidenceError(f"{location}.provenance.evidence_class is missing or unknown") from exc

    def evidence_records(self) -> list[dict[str, Any]]:
        """Return material-input provenance for diagnostics/reporting."""
        records = []
        for section in ("seasons", "rounds", "matches", "players", "lineups"):
            for record in self._payload[section]:
                records.append(
                    {"kind": section[:-1], "identity": self._record_identity(section, record), **record["provenance"]}
                )
        for match_id, stats in self._payload["player_stats"].items():
            for stat in stats:
                records.append(
                    {
                        "kind": "player_stat",
                        "identity": f"{match_id}:{stat['canonical_player_id']}",
                        **stat["provenance"],
                    }
                )
        return records

    @staticmethod
    def _record_identity(section: str, record: dict) -> str:
        keys = {
            "seasons": "season_id",
            "rounds": "round_id",
            "matches": "match_id",
            "players": "canonical_player_id",
            "lineups": "historical_entry",
        }
        return str(record[keys[section]])

    def close(self) -> None:
        pass

    def get_current_season(self) -> Season:
        current = [season for season in self._seasons.values() if season.is_current]
        if len(current) != 1:
            raise ReplayEvidenceError(f"expected exactly one current season, found {len(current)}")
        return current[0]

    def get_round(self, season_id: int, round_number: int) -> Round:
        found = [
            r
            for rid, r in self._rounds.items()
            if self._round_seasons[rid] == season_id and r.round_number == round_number
        ]
        if len(found) != 1:
            raise ReplayEvidenceError(
                f"required AFL round is missing or ambiguous: season={season_id}, round={round_number}"
            )
        return found[0]

    def get_rounds(self, season_id: int) -> list[Round]:
        if season_id not in self._seasons:
            raise ReplayEvidenceError(f"required AFL season is missing: {season_id}")
        return sorted(
            (r for rid, r in self._rounds.items() if self._round_seasons[rid] == season_id),
            key=lambda r: r.round_number,
        )

    def get_matches(self, round_id: int) -> list[Match]:
        if round_id not in self._rounds:
            raise ReplayEvidenceError(f"required AFL round evidence is missing: {round_id}")
        matches = (m for mid, m in self._matches.items() if self._match_rounds[mid] == round_id)
        return sorted((self._at_effective_time(match) for match in matches), key=lambda match: match.match_id)

    def _at_effective_time(self, match: Match) -> Match:
        timeline = self._match_records[match.match_id].get("status_timeline")
        if self.manifest.get("lifecycle_semantics") == "scheduled-start-plus-final-results-checkpoint":
            # A historical export contains the final status physically, but no
            # invented LIVE/POSTGAME transition. Scheduled time drives lockout;
            # finality is exposed only at the explicit operator checkpoint.
            status = "CONCLUDED" if self.stage == "final-results" else "UPCOMING"
            return Match(match.match_id, match.home_team, match.away_team, status, match.start_time_utc)
        if self.clock is None or timeline is None:
            return match
        status = None
        effective_at = self.clock.now()
        for transition in timeline:
            try:
                at = ReplayClock.from_iso(transition["effective_at"]).now()
                candidate = transition["status"]
            except (KeyError, TypeError) as exc:
                raise ReplayEvidenceError(f"malformed status timeline for match {match.match_id}") from exc
            if at <= effective_at:
                status = candidate
        if status is None:
            raise ReplayEvidenceError(
                f"match {match.match_id} has no status evidence at replay time {effective_at.isoformat()}"
            )
        return Match(match.match_id, match.home_team, match.away_team, status, match.start_time_utc)

    def get_player(self, canonical_player_id: int) -> Player:
        try:
            return self._players[canonical_player_id]
        except KeyError as exc:
            raise ReplayEvidenceError(f"required player identity is missing: {canonical_player_id}") from exc

    def get_match_player_stats(self, match_id: int) -> dict[int, PlayerStatLine]:
        if (
            self.manifest.get("lifecycle_semantics") == "scheduled-start-plus-final-results-checkpoint"
            and self.stage != "final-results"
        ):
            raise ReplayEvidenceError(
                f"final player stats for match {match_id} are unavailable before final-results checkpoint"
            )
        try:
            return dict(self._stats[match_id])
        except KeyError as exc:
            raise ReplayEvidenceError(f"required player-stat evidence is missing: match={match_id}") from exc


def write_replay_report(report: dict[str, Any], json_path: str | Path, summary_path: str | Path) -> None:
    """Write stable JSON plus a concise operator-readable summary."""
    required = (
        "run",
        "mapping",
        "lineups",
        "lockout",
        "scoring",
        "scorer_workflow",
        "official_results",
        "ladder",
        "discrepancies",
    )
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


def build_completed_round_report(
    database,
    lifecycle,
    round_id: str,
    evidence: ReplayAflDataSource,
    *,
    clocks: dict[str, ReplayClock],
    lockout: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    discrepancies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the replay report from a completed production round.

    This is deliberately a read-model/diagnostics function: scores, official
    versions, and ladder rows are read from their authoritative services and
    never recalculated here.
    """
    context = database.execute(
        "SELECT l.fixture_round_number,l.afl_season_id,l.afl_round_id,l.state,c.season_id,c.competition_id "
        "FROM bbbffl_round_lifecycle l JOIN competition_stream c ON c.competition_id=l.competition_id "
        "WHERE l.bbbffl_round_id=?",
        (round_id,),
    ).fetchone()
    if context is None or context["state"] != "final":
        raise ReplayEvidenceError("replay report requires a final production round")
    lineup_rows = database.execute(
        "SELECT l.lineup_id,l.season_entry_id,n.team_name,c.display_name,s.actor_type,s.actor_role,s.source_type,"
        "s.source_detail,s.reason FROM weekly_lineup l JOIN season_entry_team_name_history n "
        "ON n.season_entry_id=l.season_entry_id AND n.ended_at IS NULL JOIN season_entry_coach_history h "
        "ON h.season_entry_id=l.season_entry_id AND h.ended_at IS NULL JOIN coach c ON c.coach_id=h.coach_id "
        "JOIN weekly_lineup_submission s ON s.lineup_id=l.lineup_id AND s.version=l.effective_submission_version "
        "WHERE l.bbbffl_round_id=? ORDER BY n.team_name",
        (round_id,),
    ).fetchall()
    lineups = []
    for row in lineup_rows:
        slots = database.execute(
            "SELECT s.position,p.display_name,p.canonical_player_id FROM weekly_lineup_submission_slot s "
            "JOIN weekly_lineup l ON l.lineup_id=s.lineup_id JOIN season_player_pool p "
            "ON p.season_player_id=s.season_player_id WHERE s.lineup_id=? "
            "AND s.version=l.effective_submission_version ORDER BY s.position",
            (row["lineup_id"],),
        ).fetchall()
        manifest_lineup = next(x for x in evidence.lineup_inputs if x["historical_entry"] == row["team_name"])
        lineups.append(
            {
                "historical_entry": row["team_name"],
                "historical_coach": row["display_name"],
                "replay_actor": {"type": row["actor_type"], "role": row["actor_role"]},
                "source_type": row["source_type"],
                "source_detail": row["source_detail"],
                "reason": row["reason"],
                "provenance": manifest_lineup["provenance"],
                "positions": {
                    slot["position"]: {
                        "player": slot["display_name"],
                        "canonical_player_id": slot["canonical_player_id"],
                    }
                    for slot in slots
                },
            }
        )
    scoring, official_results = [], []
    team_by_entry = {row["season_entry_id"]: row["team_name"] for row in lineup_rows}

    def report_side(calculated: dict[str, Any], frozen: dict[str, Any]) -> dict[str, Any]:
        frozen_slots = {slot["slot"]: slot for slot in frozen["slots"]}
        interchange_evidence = next((slot for slot in calculated["slots"] if slot["position"] == "Interchange"), None)
        return {
            "historical_entry": team_by_entry[frozen["season_entry_id"]],
            "calculated_score": frozen["calculated_score"],
            "official_score": frozen["effective_score"],
            "slots": [
                {
                    "slot": slot["position"],
                    "canonical_player_id": slot["canonical_player_id"],
                    "afl_match_id": slot["afl_match_id"],
                    "calculated_score": slot["score"],
                    "official_score": frozen_slots[slot["position"]]["effective_score"],
                    "scoring_source": slot["scoring_source"],
                    "source_afl_round_id": slot["source_afl_round_id"],
                    "participation": slot["participation"],
                    "dnp_ruling": frozen_slots[slot["position"]]["dnp_ruling"],
                }
                for slot in calculated["slots"]
                if slot["position"] != "Interchange"
            ],
            "interchange": {
                "canonical_player_id": frozen["interchange"]["canonical_player_id"],
                "played": frozen["interchange"]["played"],
                "dnp_ruling": frozen["interchange"]["dnp_ruling"],
                "target_position": frozen["interchange"]["target_position"],
                "potential_scores": frozen["interchange"]["potential_scores"],
                "scoring_source": interchange_evidence["scoring_source"] if interchange_evidence else None,
                "source_afl_round_id": interchange_evidence["source_afl_round_id"] if interchange_evidence else None,
            },
        }

    for order, matchup in enumerate(lifecycle.list_matchups(round_id), start=1):
        result = lifecycle.effective_result(matchup.matchup_id)
        calculation = database.execute(
            "SELECT snapshot FROM bbbffl_matchup_calculation WHERE matchup_id=?", (matchup.matchup_id,)
        ).fetchone()
        if calculation is None:
            raise ReplayEvidenceError(f"completed matchup {order} has no calculation snapshot")
        calculated = json.loads(calculation["snapshot"])
        scoring.append(
            {
                "matchup_order": order,
                "home": report_side(calculated["home"], result.input_snapshot["home"]),
                "away": report_side(calculated["away"], result.input_snapshot["away"]),
            }
        )
        official_results.append(
            {
                "matchup_order": order,
                "result_version": f"matchup-{order}:v{result.version}",
                "version": result.version,
                "effective": True,
                "home_score": str(result.home_score),
                "away_score": str(result.away_score),
                "outcome": "draw"
                if result.home_score == result.away_score
                else "home"
                if result.home_score > result.away_score
                else "away",
            }
        )
    ladder = LadderRepository(database).snapshot(context["competition_id"], context["fixture_round_number"])
    return {
        "run": {
            "run_id": evidence.manifest["id"],
            "season": int(evidence.manifest["bbbffl_season"]),
            "round": int(evidence.manifest["bbbffl_round"]),
            "evidence_manifest": evidence.manifest["id"],
            "evidence_version": evidence.manifest["version"],
            "configuration": {"afl_mode": "replay", "schema": evidence.SCHEMA},
            "effective_times": {key: value.now().isoformat() for key, value in sorted(clocks.items())},
        },
        "mapping": {
            "bbbffl_round": context["fixture_round_number"],
            "bbbffl_fixture": [
                {
                    "matchup_order": matchup["matchup_order"],
                    "home": matchup["home"]["historical_entry"],
                    "away": matchup["away"]["historical_entry"],
                }
                for matchup in scoring
            ],
            "afl_season_id": context["afl_season_id"],
            "afl_round_id": context["afl_round_id"],
            "afl_matches": [match.match_id for match in evidence.get_matches(context["afl_round_id"])],
            "exceptional": False,
        },
        "lineups": lineups,
        "validation": validation,
        "lockout": lockout,
        "scoring": scoring,
        "scorer_workflow": {
            "unresolved_inputs": [],
            "rulings": [],
            "signoff": {"actor_type": "anonymous_operator", "actor_role": "scorer", "state": context["state"]},
        },
        "official_results": official_results,
        "ladder": [
            {
                "rank": row.rank,
                "team": team_by_entry[row.season_entry_id],
                "played": row.played,
                "wins": row.wins,
                "draws": row.draws,
                "losses": row.losses,
                "points": row.competition_points,
                "percentage": str(row.percentage),
                "points_for": str(row.points_for),
                "tie_group": sorted(team_by_entry[entry_id] for entry_id in row.tie_group),
            }
            for row in ladder.rows
        ],
        "evidence": evidence.evidence_records(),
        "discrepancies": list(discrepancies or []),
    }
