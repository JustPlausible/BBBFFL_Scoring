"""Operator CLI for issue #192's entry-scoped SuperScore review rulings
(`app.superscore_review.SuperScoreReviewRepository`).

Issue #194 found this repository -- DNP/Interchange/override rulings, the
entry-scoped counterpart of `app/routes/round_review.py`'s matchup-keyed
DNP/Interchange/override routes -- had no operator-reachable entry point at
all: no HTTP route, no CLI, only `tests/test_superscore_round.py` exercised
it. It is fully implemented and tested; only the operator surface was
missing. This script adds exactly that surface, mirroring `scripts/finals_
bracket_2026.py`'s shape, and changes nothing in `app.superscore_review`.

Usage
-----

    cd bbbffl_app
    python -m scripts.superscore_review_2026 --database-url ... \\
        dnp --round-id <id> --season-entry-id <id> --slot F1 --dnp true \\
            --expected-review-version 1 --reason "..."
    python -m scripts.superscore_review_2026 --database-url ... \\
        interchange --round-id <id> --season-entry-id <id> --target-position F1 \\
            --expected-review-version 2 --reason "..."
    python -m scripts.superscore_review_2026 --database-url ... \\
        override --round-id <id> --season-entry-id <id> --position F1 \\
            --override-score 12.5 --calculated-score 4.0 \\
            --expected-review-version 3 --reason "..."
    python -m scripts.superscore_review_2026 --database-url ... \\
        status --round-id <id> --season-entry-id <id>

Every mutating subcommand requires `--expected-review-version`, the review-
state version the round's own leaderboard/scorer surface last reported for
this entry (`app.superscore_round.get_review_state`, or this script's own
`status`) -- a stale value is rejected (`StaleReviewVersionError`) rather
than silently applied against a ruling/lineup state the operator has not
actually seen, exactly like every other CAS-protected write in this
replay's operator tooling.

`interchange --no-coverage` and `override --clear` (in place of
`--target-position`/`--override-score` respectively) record the domain's
own explicit "no coverage"/"remove this override" states
(`target_position=None`/`override_score=None`) -- both genuinely supported
by `app.superscore_review.SuperScoreReviewRepository` itself, not merely
omissions this CLI happens not to expose (Codex review, P2).

Every mutating subcommand also refuses once the owning season is
`completed` (`SeasonCompletedError`) -- the same completed-season write
fence `app.superscore_results`/`app.finals_review`/`app.calculations`
already enforce, added to `app.superscore_review` directly by issue #194
after Codex review (P1) found review state remained writable through this
CLI even after `scripts.season_completion_2026 complete` had run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.audit import ActorContext
from app.db import connect
from app.migrations import migrate
from app.season import SeasonCompletedError
from app.superscore_review import SuperScoreReviewError, SuperScoreReviewRepository
from app.superscore_round import get_review_state

ACTOR = ActorContext.anonymous_operator("replay_operator")


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _parse_bool(raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ValueError(f"--dnp must be true or false, not {raw!r}")


def cmd_dnp(database, args: argparse.Namespace) -> int:
    reviews = SuperScoreReviewRepository(database)
    new_version = reviews.record_dnp_ruling(
        args.round_id,
        args.season_entry_id,
        args.slot,
        _parse_bool(args.dnp),
        expected_review_version=args.expected_review_version,
        actor=ACTOR,
        reason=args.reason,
    )
    _print({"round_id": args.round_id, "season_entry_id": args.season_entry_id, "review_version": new_version})
    return 0


def cmd_interchange(database, args: argparse.Namespace) -> int:
    # `--no-coverage` is the CLI spelling of the domain's own
    # `target_position=None` -- an explicit "this vacant/DNP slot is not
    # covered by an interchange" ruling, required to clear the otherwise
    # unresolved-interchange publication blocker (Codex review, P2).
    target_position = None if args.no_coverage else args.target_position
    reviews = SuperScoreReviewRepository(database)
    new_version = reviews.record_interchange_ruling(
        args.round_id,
        args.season_entry_id,
        target_position,
        expected_review_version=args.expected_review_version,
        actor=ACTOR,
        reason=args.reason,
    )
    _print(
        {
            "round_id": args.round_id,
            "season_entry_id": args.season_entry_id,
            "target_position": target_position,
            "review_version": new_version,
        }
    )
    return 0


def cmd_override(database, args: argparse.Namespace) -> int:
    # `--clear` is the CLI spelling of the domain's own `override_score=
    # None` -- the supported correction for an override later found
    # unnecessary, restoring calculated scoring (Codex review, P2).
    # `--reason` is still passed through either way (the domain layer only
    # requires it when *setting* an override, via `MissingOverrideReasonError`
    # -- it remains a legitimate, recorded audit reason for a clear too).
    override_score = None if args.clear else args.override_score
    calculated_score = None if args.clear else args.calculated_score
    reviews = SuperScoreReviewRepository(database)
    new_version = reviews.record_override(
        args.round_id,
        args.season_entry_id,
        args.position,
        override_score,
        calculated_score,
        args.reason,
        expected_review_version=args.expected_review_version,
        actor=ACTOR,
    )
    _print(
        {
            "round_id": args.round_id,
            "season_entry_id": args.season_entry_id,
            "override_score": override_score,
            "review_version": new_version,
        }
    )
    return 0


def cmd_status(database, args: argparse.Namespace) -> int:
    reviews = SuperScoreReviewRepository(database)
    # Codex review (P2): a recorded ruling with target_position=None
    # (explicit "no coverage", via --no-coverage) and no ruling recorded at
    # all both used to print as bare `null` here, indistinguishably -- only
    # the former actually clears the unresolved-interchange publication
    # blocker, so an operator reading this status could not tell which one
    # they were looking at. `recorded` makes that distinction explicit.
    interchange = reviews.get_interchange_ruling(args.round_id, args.season_entry_id)
    _print(
        {
            "round_id": args.round_id,
            "season_entry_id": args.season_entry_id,
            "review_version": get_review_state(database, args.round_id, args.season_entry_id),
            "slot_rulings": {
                slot: {"dnp": ruling.dnp}
                for slot, ruling in reviews.get_slot_rulings(args.round_id, args.season_entry_id).items()
            },
            "interchange_ruling": {
                "recorded": interchange is not None,
                "target_position": interchange.target_position if interchange is not None else None,
            },
            "overrides": {
                position: override.override_score
                for position, override in reviews.get_overrides(args.round_id, args.season_entry_id).items()
            },
        }
    )
    return 0


COMMANDS = {"dnp": cmd_dnp, "interchange": cmd_interchange, "override": cmd_override, "status": cmd_status}
_MUTATING = {"dnp", "interchange", "override"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    top = parser.add_subparsers(dest="command", required=True)

    dnp = top.add_parser("dnp", help="record (or reverse) a DNP ruling for one entry's slot")
    dnp.add_argument("--round-id", required=True)
    dnp.add_argument("--season-entry-id", required=True)
    dnp.add_argument("--slot", required=True)
    dnp.add_argument("--dnp", required=True, help="true or false")
    dnp.add_argument("--expected-review-version", type=int, required=True)
    dnp.add_argument("--reason", required=True)

    interchange = top.add_parser("interchange", help="record an interchange target-position ruling")
    interchange.add_argument("--round-id", required=True)
    interchange.add_argument("--season-entry-id", required=True)
    interchange_target = interchange.add_mutually_exclusive_group(required=True)
    interchange_target.add_argument("--target-position", default=None)
    interchange_target.add_argument(
        "--no-coverage", action="store_true", help="explicit 'this slot is not covered' ruling (target_position=None)"
    )
    interchange.add_argument("--expected-review-version", type=int, required=True)
    interchange.add_argument("--reason", required=True)

    override = top.add_parser("override", help="record or clear a manual score override for one slot")
    override.add_argument("--round-id", required=True)
    override.add_argument("--season-entry-id", required=True)
    override.add_argument("--position", required=True)
    override_value = override.add_mutually_exclusive_group(required=True)
    override_value.add_argument("--override-score", type=float, default=None)
    override_value.add_argument(
        "--clear", action="store_true", help="remove an existing override, restoring calculated scoring"
    )
    override.add_argument(
        "--calculated-score",
        type=float,
        default=None,
        help="the calculated score at the time of override, for provenance (recommended with --override-score); unused with --clear",
    )
    override.add_argument("--expected-review-version", type=int, required=True)
    override.add_argument(
        "--reason",
        default=None,
        help="required when setting an override (enforced by app.superscore_review); optional but still recorded with --clear",
    )

    status = top.add_parser("status", help="read-only report of one entry's current review state; never mutates")
    status.add_argument("--round-id", required=True)
    status.add_argument("--season-entry-id", required=True)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print(
            "Refusing to run the SuperScore review-ruling operator CLI while BBBFFL_ENVIRONMENT=production.",
            file=sys.stderr,
        )
        return 1

    if args.command in _MUTATING:
        migrate(args.database_url)
    database = connect(args.database_url)
    try:
        return COMMANDS[args.command](database, args)
    except (SuperScoreReviewError, ValueError, SeasonCompletedError) as exc:
        print(f"superscore review operation refused: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
