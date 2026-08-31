"""Supported exporter for hermetic 2026 first-half AFL replay evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.replay import EvidenceClass, ReplayAflDataSource, ReplayEvidenceError


class ConsumerApi(Protocol):
    def get(self, path: str) -> dict | list: ...


def _rows(payload: Any, key: str, *, path: str) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get(key, payload.get("results"))
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ReplayEvidenceError(f"malformed consumer API response at {path}: expected {key} list")
    return payload


def _prov(path: str) -> dict[str, str]:
    return {"source": f"afl-api-v1:{path}", "evidence_class": EvidenceClass.KNOWN_FACT.value}


def acquire_first_half_2026(api: ConsumerApi, *, source_base_url: str, acquired_at: datetime | None = None) -> dict:
    """Acquire Opening Round and rounds 1--9; fail before returning partial evidence."""
    acquired_at = acquired_at or datetime.now(timezone.utc)
    if acquired_at.tzinfo is None:
        raise ReplayEvidenceError("acquisition timestamp must be timezone-aware")
    seasons = _rows(api.get("/api/v1/seasons"), "seasons", path="/api/v1/seasons")
    candidates = [s for s in seasons if s.get("year") == 2026]
    if len(candidates) != 1:
        raise ReplayEvidenceError(f"expected exactly one AFL 2026 season, found {len(candidates)}")
    season = candidates[0]
    season_id = season.get("season_id")
    if season_id is None:
        raise ReplayEvidenceError("AFL 2026 season is missing season_id")
    rounds_path = f"/api/v1/seasons/{season_id}/rounds"
    all_rounds = _rows(api.get(rounds_path), "rounds", path=rounds_path)

    def wanted(row: dict) -> bool:
        number = row.get("round_number")
        label = " ".join(str(row.get(k, "")) for k in ("name", "abbreviation")).strip().lower()
        return number in range(1, 10) or number == 0 or "opening round" in label

    rounds = [r for r in all_rounds if wanted(r)]
    ordinary = {r.get("round_number") for r in rounds if r.get("round_number") in range(1, 10)}
    opening = [r for r in rounds if r.get("round_number") == 0 or "opening round" in str(r.get("name", "")).lower()]
    if ordinary != set(range(1, 10)) or len(opening) != 1:
        raise ReplayEvidenceError(
            f"AFL season {season_id} requires one Opening Round and rounds 1-9; "
            f"found ordinary={sorted(ordinary)}, opening={len(opening)}"
        )
    rounds.sort(key=lambda r: (r.get("round_number", 999), r.get("round_id", 0)))
    matches_out, stats_out, players, rosters, roster_missing = [], {}, {}, {}, []
    for round_row in rounds:
        round_id = round_row.get("round_id")
        if round_id is None:
            raise ReplayEvidenceError("selected AFL round is missing round_id")
        path = f"/api/v1/rounds/{round_id}/matches"
        matches = _rows(api.get(path), "matches", path=path)
        if not matches:
            raise ReplayEvidenceError(f"AFL round {round_id} contains no matches")
        for match in matches:
            match_id = match.get("match_id")
            if match_id is None:
                raise ReplayEvidenceError(f"AFL round {round_id} contains a match missing match_id")
            if match.get("round_id", round_id) != round_id:
                raise ReplayEvidenceError(f"AFL match {match_id} references inconsistent round {match.get('round_id')}")
            for field in ("home_team", "away_team", "start_time_utc", "status"):
                if not match.get(field):
                    raise ReplayEvidenceError(f"AFL match {match_id} is missing required {field}")
            try:
                datetime.fromisoformat(str(match["start_time_utc"]).replace("Z", "+00:00"))
                for side in ("home_team", "away_team"):
                    int(match[side]["team_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ReplayEvidenceError(f"AFL match {match_id} has malformed start/team identity: {exc}") from exc
            matches_out.append({**match, "round_id": round_id, "provenance": _prov(path)})
            stats_path = f"/api/v1/matches/{match_id}/player-stats"
            stat_rows = _rows(api.get(stats_path), "players", path=stats_path)
            if not stat_rows:
                raise ReplayEvidenceError(f"required final player stats missing for AFL match {match_id}")
            exported_stats = []
            for row in stat_rows:
                pid = row.get("canonical_player_id")
                if pid is None or row.get("team_id") is None or not isinstance(row.get("stats"), dict):
                    raise ReplayEvidenceError(
                        f"AFL match {match_id} has malformed player-stat identity for player {pid}"
                    )
                stat = row["stats"]
                exported_stats.append(
                    {
                        "canonical_player_id": pid,
                        **{k: stat.get(k) for k in ("goals", "behinds", "disposals", "marks", "hitouts", "tackles")},
                        "identifiers": row.get("identifiers", {}),
                        "provenance": _prov(stats_path),
                    }
                )
                players[pid] = {
                    "canonical_player_id": pid,
                    "display_name": row.get("display_name", f"Player {pid}"),
                    "team_id": row["team_id"],
                    "team_name": row.get("team_name", ""),
                    "identifiers": row.get("identifiers", {}),
                    "provenance": _prov(stats_path),
                }
            stats_out[str(match_id)] = exported_stats
            roster_path = f"/api/v1/matches/{match_id}/rosters"
            try:
                roster = api.get(roster_path)
            except Exception as exc:  # optional endpoint: absence is coverage, never invented evidence
                rosters[str(match_id)] = None
                roster_missing.append({"match_id": match_id, "reason": type(exc).__name__})
            else:
                rosters[str(match_id)] = roster
    source = urlsplit(source_base_url)
    safe_source = f"{source.scheme}://{source.hostname}" + (f":{source.port}" if source.port else "")
    included = [
        {
            "round_id": r["round_id"],
            "round_number": r.get("round_number"),
            "name": r.get("name"),
            "abbreviation": r.get("abbreviation"),
        }
        for r in rounds
    ]
    return {
        "schema": ReplayAflDataSource.SCHEMA,
        "manifest": {
            "id": "afl-2026-first-half",
            "version": "1",
            "package_version": "bbbffl.first-half/v1",
            "evidence_class": EvidenceClass.KNOWN_FACT.value,
            "acquired_at": acquired_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "afl_season": 2026,
            "source_api": safe_source,
            "source_api_version": "v1",
            "exporter": "bbbffl replay acquisition v1",
            "included_rounds": included,
            "match_count": len(matches_out),
            "player_stat_match_count": len(stats_out),
            "roster_coverage": {"available": len(matches_out) - len(roster_missing), "unavailable": roster_missing},
            "lifecycle_semantics": "scheduled-start-plus-final-results-checkpoint",
        },
        "seasons": [
            {
                "season_id": season_id,
                "year": 2026,
                "is_current": True,
                "current_round_number": season.get("current_round_number"),
                "identifiers": season.get("identifiers", {}),
                "provenance": _prov("/api/v1/seasons"),
            }
        ],
        "rounds": [{**r, "season_id": season_id, "provenance": _prov(rounds_path)} for r in rounds],
        "matches": matches_out,
        "players": sorted(players.values(), key=lambda p: p["canonical_player_id"]),
        "player_stats": stats_out,
        "rosters": rosters,
        "lineups": [],
    }


def write_package(payload: dict, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def package_summary(source: ReplayAflDataSource) -> str:
    m = source.manifest
    return (
        f"validation PASS\nseason: {m.get('afl_season')}\nrounds: {len(m.get('included_rounds', []))}\n"
        f"matches: {m.get('match_count')}\nstats coverage: {m.get('player_stat_match_count')}/{m.get('match_count')}\n"
        f"roster coverage: {m.get('roster_coverage')}\npackage: {m.get('package_version')}\nacquired: {m.get('acquired_at')}"
    )
