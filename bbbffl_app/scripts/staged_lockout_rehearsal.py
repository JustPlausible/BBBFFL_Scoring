"""Deterministic staged progressive-lockout rehearsal for GitHub issue #91.

This deliberately extends the issue #85 persistent replay bootstrap rather
than replacing it.  ``bootstrap`` creates the ordinary browser-ready round;
``advance`` rewrites only its synthetic replay evidence to a named stage.
Restart the application after advancing because replay evidence is eagerly
loaded at process start.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.audit import ActorContext
from app.db import connect, transaction
from app.lineups import POSITIONS
from app.lockouts import LockoutTriggerRepository
from scripts.bootstrap_round1_2026 import BootstrapResult, bootstrap_round1_2026

STAGES = ("initial", "selective-a", "selective-b", "main")
MATCH_IDS = (9101, 9102, 9103, 9104, 9105)
TEAM_IDS = (1101, 1201, 1301, 1401, 1501)
STARTS = (
    "2035-03-01T08:00:00Z",
    "2035-03-02T08:00:00Z",
    "2035-03-03T08:00:00Z",
    "2035-02-28T08:00:00Z",  # uncovered: chronology must not imply a BBBFFL lock
    "2035-03-04T08:00:00Z",
)
POSITION_MATCH = dict(zip(POSITIONS, (9101, 9102, 9103, 9104, 9105, 9105, 9105, 9105, 9105), strict=True))


@dataclass(frozen=True)
class StagedRehearsalResult:
    bootstrap: BootstrapResult
    position_match_ids: dict[str, int]


def _statuses(stage: str) -> dict[int, str]:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; choose one of {', '.join(STAGES)}")
    statuses = {match_id: "UPCOMING" for match_id in MATCH_IDS}
    # Match 4 is deliberately already live at every stage but is absent from
    # every selective trigger. Its player remains editable until main.
    statuses[9104] = "LIVE"
    if STAGES.index(stage) >= 1:
        statuses[9101] = "LIVE"
    if STAGES.index(stage) >= 2:
        statuses[9102] = "LIVE"  # Match 3 stays UPCOMING: grouped activation is the point.
    if STAGES.index(stage) >= 3:
        statuses[9105] = "LIVE"
    return statuses


def advance_evidence(evidence_path: str | Path, stage: str) -> None:
    """Move replay evidence to ``stage`` without sleeps or wall-clock time."""
    path = Path(evidence_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    statuses = _statuses(stage)
    for match in payload["matches"]:
        match["status"] = statuses[int(match["match_id"])]
    payload["manifest"]["staged_lockout_stage"] = stage
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def bootstrap_staged_lockout_rehearsal(database_url: str, evidence_path: str | Path) -> StagedRehearsalResult:
    base = bootstrap_round1_2026(
        database_url,
        evidence_path,
        generated_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
        lockout_in_days=365,
        afl_match_id=MATCH_IDS[-1],
    )
    path = Path(evidence_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    original_match = payload["matches"][0]
    provenance = original_match["provenance"]
    payload["manifest"].update(
        id="2026-staged-lockout-rehearsal",
        version="1.0.0",
        description="Issue #91 deterministic staged progressive-lockout rehearsal; supplements issue #85.",
    )
    payload["matches"] = [
        {
            "match_id": match_id,
            "round_id": base.afl_round_id,
            "home_team": {"team_id": team_id, "name": f"Stage Club {index}A"},
            "away_team": {"team_id": team_id + 1, "name": f"Stage Club {index}B"},
            "status": "UPCOMING",
            "start_time_utc": STARTS[index - 1],
            "provenance": provenance,
        }
        for index, (match_id, team_id) in enumerate(zip(MATCH_IDS, TEAM_IDS, strict=True), 1)
    ]
    # Every replay match needs a stats section. Scoring is not this rehearsal's
    # subject, so reuse the deterministic synthetic stat rows from #85.
    original_stats = next(iter(payload["player_stats"].values()))
    payload["player_stats"] = {str(match_id): original_stats for match_id in MATCH_IDS}

    match_team = {match_id: TEAM_IDS[MATCH_IDS.index(match_id)] for match_id in MATCH_IDS}
    player_match: dict[int, int] = {}
    for lineup in payload["lineups"]:
        for position, canonical_id in lineup["positions"].items():
            player_match[int(canonical_id)] = POSITION_MATCH[position]
    for player in payload["players"]:
        team_id = match_team[player_match[int(player["canonical_player_id"])]]
        player["team_id"] = team_id
        player["team_name"] = f"Stage Club {TEAM_IDS.index(team_id) + 1}A"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    advance_evidence(path, "initial")

    database = connect(database_url)
    try:
        # The #85 main trigger points at match 5 already; add persisted
        # selective plan stages and refresh player club resolution.
        triggers = LockoutTriggerRepository(database)
        actor = ActorContext.anonymous_operator("scorer")
        reason = "issue #91 staged progressive-lockout rehearsal"
        triggers.create(base.bbbffl_round_id, "selective-a", "selective", 1, [9101], actor=actor, reason=reason)
        triggers.create(base.bbbffl_round_id, "selective-b", "selective", 2, [9102, 9103], actor=actor, reason=reason)
        triggers.replace(
            base.bbbffl_round_id,
            "main",
            trigger_type="main",
            sequence=3,
            afl_match_ids=[9105],
            actor=actor,
            reason=f"{reason}: order main after selective stages",
        )
        with transaction(database) as conn:
            for player in payload["players"]:
                team_id = int(player["team_id"])
                conn.execute(
                    "UPDATE season_player_pool SET afl_team_id=?, afl_team_name=? "
                    "WHERE season_id=? AND canonical_player_id=?",
                    (team_id, player["team_name"], base.season_id, int(player["canonical_player_id"])),
                )
    finally:
        database.close()
    return StagedRehearsalResult(base, dict(POSITION_MATCH))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--database-url", required=True)
    bootstrap.add_argument("--evidence-path", required=True)
    advance = sub.add_parser("advance")
    advance.add_argument("stage", choices=STAGES)
    advance.add_argument("--evidence-path", required=True)
    args = parser.parse_args()
    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print("Refusing to operate rehearsal evidence in production.", file=sys.stderr)
        return 1
    if args.command == "bootstrap":
        result = bootstrap_staged_lockout_rehearsal(args.database_url, args.evidence_path)
        print(f"Staged rehearsal ready: season={result.bootstrap.season_id} round={result.bootstrap.bbbffl_round_id}")
        print("stage=initial; restart the app after each advance command")
    else:
        advance_evidence(args.evidence_path, args.stage)
        print(f"stage={args.stage}; restart the app to eagerly reload replay evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
