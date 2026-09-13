"""Operator CLI for issue #192's SuperScore stream/round lifecycle setup.

Issue #194 found that `app.superscore_round` (stream creation, round
creation, AFL-mapping confirmation, round setup, round opening) had no
operator-reachable entry point at all -- no CLI, no HTTP route -- unlike
`app.finals`/`app.finals_seeding`, which both got a CLI in issues #187/#190.
Every one of those functions was already fully implemented and tested
(`tests/test_superscore_round.py`); only the operator surface was missing.
This script adds exactly that surface, mirroring `scripts/finals_bracket_
2026.py`'s shape, and changes none of `app.superscore_round`'s domain logic
except the one PostgreSQL correctness fix issue #194 made directly in that
module (`_create_review_state_rows` no longer combines `COUNT(*)` with
`FOR UPDATE`, which real PostgreSQL rejects -- see that function's comment).

Usage
-----

`--database-url` is a top-level option and must come *before* the
subcommand name, exactly like `scripts/finals_bracket_2026.py`:

    cd bbbffl_app
    python -m scripts.superscore_round_2026 --database-url ... \\
        ensure-stream --season-id <season_id> --rules-version-id <id> \\
            --ordinary-competition-id <id> --reason "..."
    python -m scripts.superscore_round_2026 --database-url ... \\
        ensure-round --competition-id <superscore_competition_id> --round-number 1
    python -m scripts.superscore_round_2026 --database-url ... \\
        confirm-mapping --round-id <id> --afl-season-id <season> --afl-round-id <round> --reason "..."
    python -m scripts.superscore_round_2026 --database-url ... \\
        setup-round --round-id <id> --reason "..."
    python -m scripts.superscore_round_2026 --database-url ... \\
        open-round --round-id <id> --reason "..."
    python -m scripts.superscore_round_2026 --database-url ... \\
        status --round-id <id>

Per docs/2026-finals-superscore-design.md's confirmed rule, SS1-SS4 run
across the *same* four AFL rounds as the four finals weeks -- source
`--afl-season-id`/`--afl-round-id` for `confirm-mapping` from the
corresponding finals week's own accepted mapping
(`scripts/finals_bracket_2026.py`'s `create-bracket`/`open-week` output, or
`app.round_mapping.RoundMappingRepository.resolve`), never re-derive them
independently. `ensure-stream`/`ensure-round`/`confirm-mapping` are
idempotent; `setup-round` is idempotent against an already-complete review-
state set; `open-round` refuses (no mutation) unless `setup-round` has
already produced the complete ten-row review-state set for that round.
`status` never mutates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.audit import ActorContext
from app.db import connect
from app.migrations import migrate
from app.replay import ReplayAflDataSource
from app.round_mapping import AflApiReferenceValidator
from app.superscore_round import (
    SuperScoreRoundError,
    confirm_afl_mapping,
    ensure_round,
    ensure_stream,
    get_stream,
    open_round,
    review_state_complete,
    setup_round,
)

ACTOR = ActorContext.anonymous_operator("replay_operator")


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def cmd_ensure_stream(database, args: argparse.Namespace) -> int:
    stream = ensure_stream(
        database,
        args.season_id,
        args.rules_version_id,
        args.ordinary_competition_id,
        actor=ACTOR,
        reason=args.reason,
    )
    _print(
        {
            "competition_id": stream.competition_id,
            "season_id": stream.season_id,
            "ordinary_competition_id": stream.ordinary_competition_id,
        }
    )
    return 0


def cmd_ensure_round(database, args: argparse.Namespace) -> int:
    round_id = ensure_round(database, args.competition_id, args.round_number, args.round_number)
    _print({"bbbffl_round_id": round_id, "round_number": args.round_number})
    return 0


def cmd_confirm_mapping(database, args: argparse.Namespace) -> int:
    # The same `app.round_mapping.AflApiReferenceValidator` boundary every
    # stream uses, built over a `ReplayAflDataSource` reading the same
    # hermetic evidence/checkpoint package the replay app service itself
    # reads (`docs/2026-second-half-replay-playbook.md` section E) -- never
    # a live afl-api round trip, matching this CLI's replay-only scope.
    afl_client = ReplayAflDataSource(args.evidence_path, checkpoint_path=args.checkpoint_path)
    validator = AflApiReferenceValidator(afl_client)
    mapping = confirm_afl_mapping(
        database,
        validator,
        args.round_id,
        args.afl_season_id,
        args.afl_round_id,
        actor=ACTOR,
        reason=args.reason,
    )
    _print(
        {"bbbffl_round_id": args.round_id, "afl_season_id": mapping.afl_season_id, "afl_round_id": mapping.afl_round_id}
    )
    return 0


def cmd_setup_round(database, args: argparse.Namespace) -> int:
    round_row = setup_round(database, args.round_id, actor=ACTOR, reason=args.reason)
    _print({"bbbffl_round_id": round_row.bbbffl_round_id, "state": round_row.state, "review_state_complete": True})
    return 0


def cmd_open_round(database, args: argparse.Namespace) -> int:
    round_row = open_round(database, args.round_id, actor=ACTOR, reason=args.reason)
    _print({"bbbffl_round_id": round_row.bbbffl_round_id, "state": round_row.state})
    return 0


def cmd_status(database, args: argparse.Namespace) -> int:
    stream = get_stream(database, args.season_id) if args.season_id else None
    report = {
        "season_id": args.season_id,
        "stream": (
            {
                "competition_id": stream.competition_id,
                "ordinary_competition_id": stream.ordinary_competition_id,
            }
            if stream
            else None
        ),
    }
    if args.round_id:
        report["round_id"] = args.round_id
        report["review_state_complete"] = review_state_complete(database, args.round_id)
    _print(report)
    return 0


COMMANDS = {
    "ensure-stream": cmd_ensure_stream,
    "ensure-round": cmd_ensure_round,
    "confirm-mapping": cmd_confirm_mapping,
    "setup-round": cmd_setup_round,
    "open-round": cmd_open_round,
    "status": cmd_status,
}

# Every subcommand except `status` performs a real mutation.
_MUTATING = {"ensure-stream", "ensure-round", "confirm-mapping", "setup-round", "open-round"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    top = parser.add_subparsers(dest="command", required=True)

    ensure_stream_p = top.add_parser("ensure-stream", help="idempotently create the season's SuperScore stream")
    ensure_stream_p.add_argument("--season-id", required=True)
    ensure_stream_p.add_argument("--rules-version-id", required=True)
    ensure_stream_p.add_argument("--ordinary-competition-id", required=True)
    ensure_stream_p.add_argument("--reason", required=True)

    ensure_round_p = top.add_parser("ensure-round", help="idempotently create one of SS1-SS4's logical round row")
    ensure_round_p.add_argument("--competition-id", required=True, help="the superscore-typed competition_stream id")
    ensure_round_p.add_argument("--round-number", type=int, required=True, choices=(1, 2, 3, 4))

    confirm_mapping_p = top.add_parser(
        "confirm-mapping", help="accept or correct one SuperScore round's AFL-round mapping against afl-api evidence"
    )
    confirm_mapping_p.add_argument("--round-id", required=True)
    confirm_mapping_p.add_argument("--afl-season-id", type=int, required=True)
    confirm_mapping_p.add_argument("--afl-round-id", type=int, required=True)
    confirm_mapping_p.add_argument(
        "--evidence-path", required=True, help="the replay evidence JSON, e.g. 2026-second-half.json"
    )
    confirm_mapping_p.add_argument("--checkpoint-path", default=None, help="optional replay checkpoint JSON")
    confirm_mapping_p.add_argument("--reason", required=True)

    setup_round_p = top.add_parser(
        "setup-round", help="create the round's lifecycle row and its complete ten-entry review-state row set"
    )
    setup_round_p.add_argument("--round-id", required=True)
    setup_round_p.add_argument("--reason", required=True)

    open_round_p = top.add_parser("open-round", help="upcoming -> open, refusing unless review-state setup is complete")
    open_round_p.add_argument("--round-id", required=True)
    open_round_p.add_argument("--reason", required=True)

    status_p = top.add_parser("status", help="read-only report; never mutates")
    status_p.add_argument("--season-id", default=None)
    status_p.add_argument("--round-id", default=None)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print(
            "Refusing to run the SuperScore round-setup operator CLI while BBBFFL_ENVIRONMENT=production.",
            file=sys.stderr,
        )
        return 1

    if args.command in _MUTATING:
        migrate(args.database_url)
    database = connect(args.database_url)
    try:
        return COMMANDS[args.command](database, args)
    except SuperScoreRoundError as exc:
        print(f"superscore round operation refused: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
