"""Persistent 2026 Round 1 rehearsal bootstrap (issue #85).

Establishes the minimum *ordinary* application state needed to rehearse
2026 Round 1 end-to-end through the real browser routes, against a normal
persistent (SQLite or PostgreSQL) application database:

    clean DB -> migrations -> this bootstrap -> coach login/submission ->
    scorer Round Centre -> sign-off -> public Round Centre -> ladder

This is deliberately an *integration/operability* script, not a second
scoring implementation: every write below goes through the same
production repositories/services the browser routes and the hermetic
replay suite already use (`app.season`, `app.identity`, `app.fixtures`,
`app.round_mapping`, `app.competition_lifecycle`, `app.player_pool`,
`app.lockouts`, `app.lineup_proxy`, `app.auth`). See
`docs/replay-harness.md` for the shared replay evidence/provenance
conventions this reuses, and `bbbffl_app/README.md`'s "Round 1 rehearsal
quick start" for the full operator walkthrough.

## What this seeds

- the 2026 BBBFFL competition/season (a single ordinary stream, one round);
- all ten BBBFFL teams/season entries/coaches;
- Round 1, mapped to an AFL round via the normal `app.round_mapping`
  acceptance path;
- five BBBFFL Round 1 fixture matchups (`app.fixtures`);
- a player pool and squad ownership sufficient for every entry to submit a
  legal lineup;
- a configured Round 1 lockout trigger (`app.lockouts`), pointed at a
  synthetic AFL match scheduled far enough in the future that it never
  activates during an ordinary rehearsal session;
- a freshly generated, self-contained replay evidence file (see "Evidence"
  below) that the running application consumes through the existing
  `BBBFFL_AFL_MODE=replay` boundary -- never a live afl-api call;
- **nine of the ten entries'** Round 1 lineups, submitted through
  `app.lineup_proxy.LineupProxyService` exactly as a scorer/admin proxy
  entry would be (`source_type="scorer_proxy"`, truthfully reasoned as a
  bootstrap reconstruction, never presented as a genuine historical coach
  submission -- see docs/replay-harness.md's provenance rules).

The **tenth entry ("Coach A") is deliberately left unsubmitted** -- that is
the one lineup the operator submits themselves, through the real coach
login/lineup browser flow, after this script sets a test password for
Coach A. Nothing here seeds official results or advances the round past
"open": calculation, review, sign-off and the ladder all still run through
their normal services once the operator drives them from the browser.

## Evidence

The generated evidence file follows the same `bbbffl.replay-evidence/v1`
schema as `tests/fixtures/replay_round_2026/evidence.json` (see
docs/replay-harness.md), but is **not** that file: the hermetic test
fixture's single AFL match is provenance-labelled `CONCLUDED` (it exists to
prove calculation/review/sign-off/ladder against an already-finished
round), which would make every selected player instantly lockout-`locked`
the moment a real, wall-clock-driven browser session evaluates it -- there
would be no way to demonstrate the coach lineup-submission step at all.
This bootstrap's evidence instead describes its one synthetic AFL match as
`UPCOMING`, scheduled `--lockout-in-days` days after the bootstrap runs (so
its own configured lockout trigger stays open for that whole window) --
still entirely synthetic, still fully provenance-labelled, and still
consumed through the unmodified `app.replay.ReplayAflDataSource` boundary.
Every record's `provenance.evidence_class` is `synthetic_scenario` with a
source string naming this script -- never presented as genuine historical
AFL or BBBFFL fact.

## Idempotency

Refusal-based, not idempotent: this bootstrap refuses outright (raising
`RehearsalAlreadyBootstrappedError`, exit code 1 from the CLI) if a 2026
BBBFFL season already exists in the target database, so running it twice
against the same database can never silently duplicate competition/season/
team/fixture/mapping/squad state. See the error message (and the README
quick start) for how to reset a rehearsal database.

## Safety

Refuses outright when `BBBFFL_ENVIRONMENT=production`, exactly like
`scripts/replay_2026_draft.py`/`scripts/replay_2026_preseason.py` -- this
is a rehearsal tool for a dedicated, disposable database, never a
production seeding path.

## Usage

    cd bbbffl_app
    python -m scripts.bootstrap_round1_2026

See `bbbffl_app/README.md`'s Round 1 rehearsal quick start for the full
sequence (migrations, replay-mode environment variables, starting the app,
and the browser URLs to visit).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.audit import ActorContext
from app.auth import CredentialRepository
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.config import BASE_DIR
from app.db import connect
from app.fixtures import FixtureRepository
from app.identity import IdentityRepository
from app.lineup_proxy import LineupProxyService
from app.lineups import POSITIONS
from app.lockouts import LockoutTriggerRepository
from app.migrations import migrate
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.replay import ReplayAflDataSource
from app.round_mapping import AflApiReferenceValidator, RoundMappingRepository
from app.season import SeasonRepository

DEFAULT_DATABASE_PATH = BASE_DIR / "data" / "round1-2026-rehearsal.db"
DEFAULT_EVIDENCE_PATH = BASE_DIR / "data" / "round1-2026-rehearsal-evidence.json"

DEFAULT_AFL_SEASON_ID = 2026
DEFAULT_AFL_ROUND_ID = 1344  # same AFL round identity named by docs/replay-harness.md
DEFAULT_AFL_MATCH_ID = 5001
DEFAULT_LOCKOUT_IN_DAYS = 30

DEFAULT_COACH_A_EMAIL = "coach.a@rehearsal.bbbffl.local"
DEFAULT_COACH_A_PASSWORD = "Round1Rehearsal!25"  # rehearsal-only; reset it, never reuse it anywhere real

ENTRY_COUNT = 10
SQUAD_LIMIT = len(POSITIONS)  # one owned player per scoring slot, nothing spare
SYNTHETIC_CANONICAL_ID_BASE = 9_500_000  # a reserved range distinct from scripts.replay_2026_draft's own base

TEAM_ALPHA = {"team_id": 1, "name": "Rehearsal AFL Alpha"}
TEAM_BETA = {"team_id": 2, "name": "Rehearsal AFL Beta"}

_EVIDENCE_SOURCE = "BBBFFL issue #85 Round 1 rehearsal bootstrap (scripts.bootstrap_round1_2026) -- script-generated for interactive rehearsal, not a genuine historical AFL or BBBFFL fact"

_POSITION_STATS = {
    "F1": {"goals": 3, "behinds": 1, "disposals": 8, "marks": 4, "hitouts": 0, "tackles": 1},
    "F2": {"goals": 2, "behinds": 2, "disposals": 9, "marks": 3, "hitouts": 0, "tackles": 1},
    "F3": {"goals": 1, "behinds": 1, "disposals": 10, "marks": 5, "hitouts": 0, "tackles": 2},
    "M1": {"goals": 0, "behinds": 0, "disposals": 24, "marks": 6, "hitouts": 0, "tackles": 5},
    "M2": {"goals": 1, "behinds": 0, "disposals": 21, "marks": 5, "hitouts": 0, "tackles": 4},
    "M3": {"goals": 0, "behinds": 1, "disposals": 19, "marks": 4, "hitouts": 0, "tackles": 6},
    "Ruck": {"goals": 0, "behinds": 0, "disposals": 12, "marks": 3, "hitouts": 28, "tackles": 2},
    "Tackler": {"goals": 0, "behinds": 0, "disposals": 14, "marks": 2, "hitouts": 0, "tackles": 8},
    "Interchange": {"goals": 1, "behinds": 0, "disposals": 11, "marks": 2, "hitouts": 0, "tackles": 3},
}


class RehearsalAlreadyBootstrappedError(RuntimeError):
    """A 2026 BBBFFL season already exists in the target database -- see
    this module's docstring, "Idempotency". Carries an actionable operator
    message (also printed by the CLI entry point)."""


@dataclass(frozen=True)
class BootstrapEntry:
    season_entry_id: str
    coach_id: str
    team_name: str
    email: str | None


@dataclass(frozen=True)
class BootstrapResult:
    database_url: str
    evidence_path: str
    season_id: str
    competition_id: str
    bbbffl_round_id: str
    afl_season_id: int
    afl_round_id: int
    afl_match_id: int
    match_start_time_utc: str
    entries: tuple[BootstrapEntry, ...]
    coach_a: BootstrapEntry
    coach_a_password: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _provenance(source: str = _EVIDENCE_SOURCE) -> dict:
    return {"evidence_class": "synthetic_scenario", "source": source}


def _build_evidence_payload(
    *,
    afl_season_id: int,
    afl_round_id: int,
    afl_match_id: int,
    match_start_time_utc: str,
    team_names: list[str],
) -> dict:
    """Build a `bbbffl.replay-evidence/v1` payload for exactly one
    synthetic, still-`UPCOMING` AFL match covering both synthetic AFL
    clubs -- see this module's docstring, "Evidence", for why this must
    not simply reuse `tests/fixtures/replay_round_2026/evidence.json`."""
    players, lineups = [], []
    for entry_index, team_name in enumerate(team_names):
        positions = {}
        for position_index, position in enumerate(POSITIONS):
            slot_index = entry_index * len(POSITIONS) + position_index
            canonical_player_id = SYNTHETIC_CANONICAL_ID_BASE + slot_index
            team = TEAM_ALPHA if slot_index % 2 == 0 else TEAM_BETA
            players.append(
                {
                    "canonical_player_id": canonical_player_id,
                    "display_name": f"Rehearsal Player {slot_index + 1}",
                    "team_id": team["team_id"],
                    "team_name": team["name"],
                    "provenance": _provenance(),
                }
            )
            positions[position] = canonical_player_id
        lineups.append({"historical_entry": team_name, "positions": positions, "provenance": _provenance()})

    player_stats = [
        {"canonical_player_id": player["canonical_player_id"], **_POSITION_STATS[position], "provenance": _provenance()}
        for player, position in zip(players, POSITIONS * len(team_names), strict=True)
    ]

    return {
        "schema": ReplayAflDataSource.SCHEMA,
        "manifest": {
            "id": "2026-round-1-rehearsal-bootstrap",
            "version": "1.0.0",
            "evidence_class": "synthetic_scenario",
            "bbbffl_season": 2026,
            "bbbffl_round": 1,
            "description": (
                "Persistent, interactive Round 1 rehearsal evidence (issue #85) -- distinct from the hermetic "
                "one-round replay test fixture; see docs/replay-harness.md."
            ),
        },
        "seasons": [
            {
                "season_id": afl_season_id,
                "year": afl_season_id,
                "is_current": True,
                "current_round_number": 1,
                "provenance": _provenance(),
            }
        ],
        "rounds": [
            {
                "round_id": afl_round_id,
                "season_id": afl_season_id,
                "round_number": 1,
                "byes": [],
                "provenance": _provenance(),
            }
        ],
        "matches": [
            {
                "match_id": afl_match_id,
                "round_id": afl_round_id,
                "home_team": TEAM_ALPHA,
                "away_team": TEAM_BETA,
                "status": "UPCOMING",
                "start_time_utc": match_start_time_utc,
                "provenance": _provenance(),
            }
        ],
        "players": players,
        "player_stats": {str(afl_match_id): player_stats},
        "lineups": lineups,
    }


def bootstrap_round1_2026(
    database_url: str,
    evidence_path: str | Path,
    *,
    coach_a_email: str = DEFAULT_COACH_A_EMAIL,
    coach_a_password: str = DEFAULT_COACH_A_PASSWORD,
    lockout_in_days: float = DEFAULT_LOCKOUT_IN_DAYS,
    afl_season_id: int = DEFAULT_AFL_SEASON_ID,
    afl_round_id: int = DEFAULT_AFL_ROUND_ID,
    afl_match_id: int = DEFAULT_AFL_MATCH_ID,
    generated_at: datetime | None = None,
) -> BootstrapResult:
    """Run the full bootstrap against `database_url`. Raises
    `RehearsalAlreadyBootstrappedError` (see this module's docstring,
    "Idempotency") without writing anything if a 2026 season already
    exists."""
    evidence_path = Path(evidence_path)
    migrate(database_url)
    database = connect(database_url)
    try:
        seasons = SeasonRepository(database)
        if seasons.get_season_by_year(2026) is not None:
            raise RehearsalAlreadyBootstrappedError(
                "A 2026 BBBFFL season already exists in this database -- refusing to bootstrap a second, "
                "duplicate Round 1 rehearsal (competition/season/team/fixture/mapping state is never "
                "silently duplicated). To start over: point --database-url at a fresh database (for SQLite, "
                f"delete the file, e.g. `rm {database_url.removeprefix('sqlite:///')}`, then re-run "
                "`python -m app.migrations upgrade`; for PostgreSQL, drop and recreate the rehearsal database "
                "and re-run migrations), then re-run this bootstrap."
            )

        operator = ActorContext.anonymous_operator(role="scorer")
        reason = "2026 Round 1 rehearsal bootstrap (issue #85)"

        season = seasons.create_season(2026, "2026 BBBFFL Season (Round 1 rehearsal)", regular_season_round_count=1)
        rules = seasons.create_rules_version(season.season_id, "ordinary", 1, "2026 ordinary rules")
        competition = seasons.create_competition(
            season.season_id, rules.rules_version_id, "ordinary", "2026 Ordinary Season", "ordinary"
        )
        logical_round = seasons.create_round(competition.competition_id, "round-1", "Round 1", 1)

        identities = IdentityRepository(database)
        team_names = [f"Rehearsal Team {n}" for n in range(1, ENTRY_COUNT + 1)]
        entries: list[BootstrapEntry] = []
        for n, team_name in enumerate(team_names, 1):
            is_coach_a = n == 1
            display_name = (
                "Coach A (Round 1 rehearsal)" if is_coach_a else f"Coach {n} (Round 1 rehearsal, scorer proxy)"
            )
            coach = identities.create_coach(display_name, email=coach_a_email if is_coach_a else None)
            season_entry = identities.create_entry(
                season.season_id, f"round1-2026-rehearsal-{n}", coach.coach_id, team_name, actor=operator, reason=reason
            )
            entries.append(
                BootstrapEntry(
                    season_entry.season_entry_id, coach.coach_id, team_name, coach_a_email if is_coach_a else None
                )
            )
        coach_a = entries[0]

        fixtures = FixtureRepository(database)
        fixtures.save_draft(
            season.season_id, [entry.season_entry_id for entry in entries], actor=operator, reason=reason
        )
        fixtures.freeze(season.season_id, actor=operator, reason=reason)

        generated_at = generated_at or _now()
        match_start_time_utc = (generated_at + timedelta(days=lockout_in_days)).isoformat().replace("+00:00", "Z")
        payload = _build_evidence_payload(
            afl_season_id=afl_season_id,
            afl_round_id=afl_round_id,
            afl_match_id=afl_match_id,
            match_start_time_utc=match_start_time_utc,
            team_names=team_names,
        )
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        source = ReplayAflDataSource(evidence_path)

        RoundMappingRepository(database).accept(
            logical_round.bbbffl_round_id,
            afl_season_id,
            afl_round_id,
            AflApiReferenceValidator(source),
            actor=operator,
            reason=reason,
        )

        lifecycle = CompetitionLifecycleRepository(database)
        lifecycle.create_ordinary_round(logical_round.bbbffl_round_id, actor=operator, reason=reason)
        lifecycle.transition(logical_round.bbbffl_round_id, "open", actor=operator, reason=reason)

        pool = PlayerPoolRepository(database)
        ownership = OwnershipRepository(database)
        ownership.configure_squad_limit(season.season_id, SQUAD_LIMIT, actor=operator, reason=reason)

        lineup_by_entry = {lineup["historical_entry"]: lineup for lineup in source.lineup_inputs}
        proxy = LineupProxyService(database, source)
        for entry in entries:
            positions = {}
            for position, canonical_player_id in lineup_by_entry[entry.team_name]["positions"].items():
                evidence_player = source.get_player(canonical_player_id)
                season_player = pool.refresh_player(
                    season.season_id,
                    canonical_player_id,
                    evidence_player.name,
                    afl_team_id=evidence_player.current_team.team_id,
                    afl_team_name=evidence_player.current_team.name,
                    source_provider="bbbffl-2026-round1-rehearsal-bootstrap",
                    source_fetched_at=generated_at.isoformat(),
                )
                ownership.acquire(
                    season_player.season_player_id,
                    entry.season_entry_id,
                    effective_at="2026-01-01T00:00:00+00:00",
                    actor=operator,
                    reason=reason,
                )
                positions[position] = season_player.season_player_id

            if entry is coach_a:
                # Coach A's lineup is deliberately left unsubmitted -- the
                # operator submits it themselves through the real coach
                # login/lineup browser flow (see this module's docstring).
                continue
            draft = proxy.create_or_amend(
                season.season_id,
                competition.competition_id,
                logical_round.bbbffl_round_id,
                entry.season_entry_id,
                positions,
                expected_revision=0,
                actor=operator,
            )
            proxy.submit(
                draft.lineup_id,
                expected_draft_revision=draft.revision,
                expected_submission_version=0,
                actor=operator,
                reason=(
                    "2026 Round 1 rehearsal bootstrap (issue #85): reconstructed lineup for interactive "
                    "rehearsal, not a genuine historical coach submission"
                ),
            )

        LockoutTriggerRepository(database).create(
            logical_round.bbbffl_round_id,
            "main",
            "main",
            1,
            [afl_match_id],
            actor=operator,
            reason=f"{reason}: main lockout trigger (stays open until {match_start_time_utc})",
        )

        CredentialRepository(database).set_password(
            coach_a.coach_id,
            coach_a_password,
            actor=ActorContext.anonymous_operator(role="admin"),
            reason=f"{reason}: coach A test credential",
        )

        return BootstrapResult(
            database_url=database_url,
            evidence_path=str(evidence_path),
            season_id=season.season_id,
            competition_id=competition.competition_id,
            bbbffl_round_id=logical_round.bbbffl_round_id,
            afl_season_id=afl_season_id,
            afl_round_id=afl_round_id,
            afl_match_id=afl_match_id,
            match_start_time_utc=match_start_time_utc,
            entries=tuple(entries),
            coach_a=coach_a,
            coach_a_password=coach_a_password,
        )
    finally:
        database.close()


def _print_summary(result: BootstrapResult, base_url: str) -> None:
    print("Seeded the 2026 Round 1 rehearsal (issue #85).")
    print(f"  database_url:      {result.database_url}")
    print(f"  evidence_path:     {result.evidence_path}")
    print(f"  season_id:         {result.season_id}")
    print(f"  bbbffl_round_id:   {result.bbbffl_round_id}")
    print(f"  afl_season_id/round_id/match_id: {result.afl_season_id}/{result.afl_round_id}/{result.afl_match_id}")
    print(f"  lockout opens at:  {result.match_start_time_utc} (main trigger stays open until then)")
    print()
    print("Coach A (submit this lineup yourself through the browser):")
    print(f"  team:     {result.coach_a.team_name}")
    print(f"  email:    {result.coach_a.email}")
    print(f"  password: {result.coach_a_password}")
    print()
    print("Nine other entries already have a reconstructed scorer-proxy Round 1 lineup submitted.")
    print()
    print("Next steps:")
    print("  1. Start the app against this database with replay mode pointed at the evidence file above.")
    print(f"  2. Visit {base_url}/login and sign in as Coach A.")
    print(f"  3. Visit {base_url}/account and follow the link to the Round 1 lineup.")
    print("  4. Save a draft, then submit.")
    print(f"  5. Visit {base_url}/scorer/round-centre to review, calculate and sign off all five matchups.")
    print(f"  6. Visit {base_url}/seasons/{result.season_id} for the public Round Centre and ladder.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--database-url",
        default=os.getenv("BBBFFL_REHEARSAL_DATABASE_URL", f"sqlite:///{DEFAULT_DATABASE_PATH}"),
        help=f"defaults to an isolated SQLite file at {DEFAULT_DATABASE_PATH}",
    )
    parser.add_argument(
        "--evidence-path",
        default=os.getenv("BBBFFL_REHEARSAL_EVIDENCE_PATH", str(DEFAULT_EVIDENCE_PATH)),
        help=f"where to write the generated replay evidence file (defaults to {DEFAULT_EVIDENCE_PATH})",
    )
    parser.add_argument("--coach-a-email", default=DEFAULT_COACH_A_EMAIL)
    parser.add_argument("--coach-a-password", default=DEFAULT_COACH_A_PASSWORD)
    parser.add_argument(
        "--lockout-in-days",
        type=float,
        default=DEFAULT_LOCKOUT_IN_DAYS,
        help="how many days from now the synthetic AFL match (and the round's lockout trigger) is scheduled to "
        "start; keep this comfortably longer than the rehearsal session (default: %(default)s)",
    )
    parser.add_argument("--afl-season-id", type=int, default=DEFAULT_AFL_SEASON_ID)
    parser.add_argument("--afl-round-id", type=int, default=DEFAULT_AFL_ROUND_ID)
    parser.add_argument("--afl-match-id", type=int, default=DEFAULT_AFL_MATCH_ID)
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="printed in the summary's browser URLs (default: %(default)s)",
    )
    args = parser.parse_args()

    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print("Refusing to run the Round 1 rehearsal bootstrap while BBBFFL_ENVIRONMENT=production.", file=sys.stderr)
        return 1

    try:
        result = bootstrap_round1_2026(
            args.database_url,
            args.evidence_path,
            coach_a_email=args.coach_a_email,
            coach_a_password=args.coach_a_password,
            lockout_in_days=args.lockout_in_days,
            afl_season_id=args.afl_season_id,
            afl_round_id=args.afl_round_id,
            afl_match_id=args.afl_match_id,
        )
    except RehearsalAlreadyBootstrappedError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _print_summary(result, args.base_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
