"""Operator CLI for issue #190's finals bracket generation and lifecycle.

Mirrors `scripts/finals_seeding_2026.py`'s preview/apply shape. Every
mutating subcommand requires an explicit, substantive `--reason` and is
idempotent against an unchanged resolution; every subcommand fails closed
(non-zero exit, no mutation) on any context/tie/staleness/downstream-play-
state problem rather than guessing.

Usage
-----

`--database-url` is a top-level option and must come *before* the
subcommand name:

    cd bbbffl_app
    python -m scripts.finals_bracket_2026 \\
        --database-url postgresql://bbbffl:...@database/bbbffl_2026_second_half \\
        create-bracket preview --season-id <season_id> --competition-id <finals_competition_id> \\
            --ordinary-competition-id <ordinary_competition_id>
    python -m scripts.finals_bracket_2026 \\
        --database-url ... create-bracket apply --season-id ... --competition-id ... \\
            --ordinary-competition-id ... --reason "2026 finals replay: bracket creation per issue #190"
    python -m scripts.finals_bracket_2026 --database-url ... open-week --bracket-id <id> --week 1 --reason "..."
    python -m scripts.finals_bracket_2026 --database-url ... advance preview --bracket-id <id> --from-week 1
    python -m scripts.finals_bracket_2026 --database-url ... advance apply --bracket-id <id> --from-week 1 \\
        --reason "..." --expected-versions '{"<matchup_id>": 1, "<matchup_id>": 1}'
    python -m scripts.finals_bracket_2026 --database-url ... rewind --bracket-id <id> --from-week 1 --reason "..." \\
        [--apply] [--expected-versions '{"<matchup_id>": 1}']

`preview` subcommands and `rewind` without `--apply` never mutate.

`advance apply --expected-versions` and `rewind --apply --expected-versions`
are both optional but strongly recommended: paste in the `expected_versions`
object the corresponding preview call (`advance preview`, or `rewind`
without `--apply`) printed. Without it, `apply` derives from whatever the
prerequisite matchups' official results happen to be *right now* -- if a
correction landed between your preview and apply calls, it silently
proceeds from the corrected result rather than rejecting your now-stale
preview. With it, a correction in that gap is detected under lock and
`apply` aborts (`StaleFinalsResultError`) instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.audit import ActorContext
from app.db import connect
from app.finals import (
    DownstreamPlayStateError,
    FinalsBracketError,
    FinalsBracketRepository,
    StaleFinalsResultError,
    StaleSeedOrderError,
)
from app.finals_preflight import build_finals_week_preflight, open_finals_week
from app.migrations import migrate

ACTOR = ActorContext.anonymous_operator("replay_operator")


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def cmd_create_bracket_preview(database, args: argparse.Namespace) -> int:
    report = FinalsBracketRepository(database).preview_create_bracket(
        args.season_id, args.competition_id, args.ordinary_competition_id
    )
    _print(report)
    return 0 if report["context_ready"] else 1


def cmd_create_bracket_apply(database, args: argparse.Namespace) -> int:
    result = FinalsBracketRepository(database).create_bracket(
        args.season_id, args.competition_id, args.ordinary_competition_id, actor=ACTOR, reason=args.reason
    )
    _print(result)
    if result["created"]:
        print(f"Created finals bracket {result['bracket'].bracket_id}.", file=sys.stderr)
    else:
        print(
            f"A finals bracket already exists ({result['bracket'].bracket_id}); no changes made (idempotent).",
            file=sys.stderr,
        )
    return 0


def cmd_open_week(database, args: argparse.Namespace) -> int:
    open_finals_week(database, args.bracket_id, args.week, actor=ACTOR)
    _print(build_finals_week_preflight(database, args.bracket_id, args.week))
    return 0


def cmd_advance_preview(database, args: argparse.Namespace) -> int:
    _print(FinalsBracketRepository(database).preview_advance_bracket(args.bracket_id, args.from_week))
    return 0


def cmd_advance_apply(database, args: argparse.Namespace) -> int:
    expected_versions = json.loads(args.expected_versions) if args.expected_versions else None
    result = FinalsBracketRepository(database).advance_bracket(
        args.bracket_id, args.from_week, actor=ACTOR, reason=args.reason, expected_versions=expected_versions
    )
    _print(result)
    return 0


def cmd_rewind(database, args: argparse.Namespace) -> int:
    repo = FinalsBracketRepository(database)
    expected_versions = json.loads(args.expected_versions) if args.expected_versions else None
    try:
        report = repo.rewind_bracket(
            args.bracket_id,
            args.from_week,
            actor=ACTOR,
            reason=args.reason,
            apply=args.apply,
            expected_versions=expected_versions,
        )
    except DownstreamPlayStateError as exc:
        _print(exc.report)
        print(f"rewind refused: {exc}", file=sys.stderr)
        return 1
    _print(report)
    if not args.apply:
        return 0 if not report["blocked"] else 1
    return 0


COMMANDS = {
    ("create-bracket", "preview"): cmd_create_bracket_preview,
    ("create-bracket", "apply"): cmd_create_bracket_apply,
    ("open-week", None): cmd_open_week,
    ("advance", "preview"): cmd_advance_preview,
    ("advance", "apply"): cmd_advance_apply,
    ("rewind", None): cmd_rewind,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    top = parser.add_subparsers(dest="command", required=True)

    create = top.add_parser("create-bracket", help="create (or idempotently confirm) the finals bracket")
    create_sub = create.add_subparsers(dest="mode", required=True)
    for mode in ("preview", "apply"):
        p = create_sub.add_parser(mode)
        p.add_argument("--season-id", required=True)
        p.add_argument("--competition-id", required=True, help="the finals-typed competition_stream id")
        p.add_argument("--ordinary-competition-id", required=True, help="this season's own ordinary competition_id")
        if mode == "apply":
            p.add_argument("--reason", required=True)

    open_week = top.add_parser("open-week", help="open one finals week's round after preflight")
    open_week.add_argument("--bracket-id", required=True)
    open_week.add_argument("--week", type=int, required=True, choices=(1, 2, 3, 4))

    advance = top.add_parser("advance", help="derive and persist the next week's pairing/elimination")
    advance_sub = advance.add_subparsers(dest="mode", required=True)
    for mode in ("preview", "apply"):
        p = advance_sub.add_parser(mode)
        p.add_argument("--bracket-id", required=True)
        p.add_argument("--from-week", type=int, required=True, choices=(1, 2, 3))
        if mode == "apply":
            p.add_argument("--reason", required=True)
            p.add_argument(
                "--expected-versions",
                help="JSON object of {matchup_id: official_version}, copied from 'advance preview''s own output -- "
                "when supplied, a version that changed since your preview aborts the apply instead of silently "
                "advancing from the corrected result",
            )

    rewind = top.add_parser("rewind", help="preview or apply a correction-triggered pairing/elimination rewind")
    rewind.add_argument("--bracket-id", required=True)
    rewind.add_argument("--from-week", type=int, required=True, choices=(1, 2, 3))
    rewind.add_argument("--reason", required=True)
    rewind.add_argument("--apply", action="store_true", help="omit to preview only; never mutates without this flag")
    rewind.add_argument(
        "--expected-versions",
        help="JSON object of {matchup_id: official_version}, copied from a prior rewind preview's own output -- "
        "when supplied, a version that changed since your preview aborts the apply instead of silently superseding "
        "pairings derived from the corrected result",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print("Refusing to run the finals bracket operator CLI while BBBFFL_ENVIRONMENT=production.", file=sys.stderr)
        return 1

    mode = getattr(args, "mode", None)
    handler = COMMANDS[(args.command, mode)]
    # Only a mutating call needs the migrator run first -- a preview-only
    # invocation must never upgrade the schema of a database it is only
    # meant to inspect, exactly like scripts/finals_seeding_2026.py.
    mutating = mode == "apply" or args.command == "open-week" or (args.command == "rewind" and args.apply)
    if mutating:
        migrate(args.database_url)
    database = connect(args.database_url)
    try:
        return handler(database, args)
    except (FinalsBracketError, StaleSeedOrderError, StaleFinalsResultError) as exc:
        print(f"finals bracket operation refused: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
