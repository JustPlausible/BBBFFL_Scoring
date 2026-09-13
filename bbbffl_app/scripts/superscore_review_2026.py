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
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.audit import ActorContext
from app.db import connect
from app.migrations import migrate
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
    reviews = SuperScoreReviewRepository(database)
    new_version = reviews.record_interchange_ruling(
        args.round_id,
        args.season_entry_id,
        args.target_position,
        expected_review_version=args.expected_review_version,
        actor=ACTOR,
        reason=args.reason,
    )
    _print({"round_id": args.round_id, "season_entry_id": args.season_entry_id, "review_version": new_version})
    return 0


def cmd_override(database, args: argparse.Namespace) -> int:
    reviews = SuperScoreReviewRepository(database)
    new_version = reviews.record_override(
        args.round_id,
        args.season_entry_id,
        args.position,
        args.override_score,
        args.calculated_score,
        args.reason,
        expected_review_version=args.expected_review_version,
        actor=ACTOR,
    )
    _print({"round_id": args.round_id, "season_entry_id": args.season_entry_id, "review_version": new_version})
    return 0


def cmd_status(database, args: argparse.Namespace) -> int:
    reviews = SuperScoreReviewRepository(database)
    _print(
        {
            "round_id": args.round_id,
            "season_entry_id": args.season_entry_id,
            "review_version": get_review_state(database, args.round_id, args.season_entry_id),
            "slot_rulings": {
                slot: {"dnp": ruling.dnp}
                for slot, ruling in reviews.get_slot_rulings(args.round_id, args.season_entry_id).items()
            },
            "interchange_ruling": (
                reviews.get_interchange_ruling(args.round_id, args.season_entry_id).target_position
                if reviews.get_interchange_ruling(args.round_id, args.season_entry_id)
                else None
            ),
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
    interchange.add_argument("--target-position", required=True)
    interchange.add_argument("--expected-review-version", type=int, required=True)
    interchange.add_argument("--reason", required=True)

    override = top.add_parser("override", help="record a manual score override for one slot")
    override.add_argument("--round-id", required=True)
    override.add_argument("--season-entry-id", required=True)
    override.add_argument("--position", required=True)
    override.add_argument("--override-score", type=float, required=True)
    override.add_argument("--calculated-score", type=float, required=True)
    override.add_argument("--expected-review-version", type=int, required=True)
    override.add_argument("--reason", required=True)

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
    except (SuperScoreReviewError, ValueError) as exc:
        print(f"superscore review operation refused: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
