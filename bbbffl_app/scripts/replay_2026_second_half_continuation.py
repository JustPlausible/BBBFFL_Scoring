"""Operator CLI for issue #178's 2026 second-half replay continuation.

Extends the restored, verified Phase 1 2026 season from its intentional
9-round replay-harness boundary to the full 20-round BBBFFL regular season:
Rounds 1-9 (logical rounds, frozen fixture matchups, lifecycle/submission/
result/audit history) are preserved exactly; Rounds 10-20 are appended to
the *same* frozen fixture draw from the preserved fixture-number assignments
and `app.fixtures.fixture_number_rotation`, and the missing logical Round
10-20 definitions are created. Round 10 is left unopened -- the normal Round
Preflight workflow takes over from there (see #166 and
docs/2026-second-half-replay-playbook.md).

See `app.replay_continuation` for the full domain rationale (why this
cannot be an ordinary fixture edit) and its fail-closed preflight checks.

Usage
-----

`--database-url` is a top-level option -- like `git --git-dir=... status`,
it must come *before* the subcommand name, not after it:

    cd bbbffl_app
    python -m scripts.replay_2026_second_half_continuation \\
        --database-url postgresql://bbbffl:...@database/bbbffl_2026_second_half \\
        status
    python -m scripts.replay_2026_second_half_continuation \\
        --database-url postgresql://bbbffl:...@database/bbbffl_2026_second_half \\
        continue --reason "2026 second-half replay: continuation per issue #178"

`status` never mutates. `continue` is idempotent: re-running it against an
already-continued database reports success without creating duplicate rows,
and refuses with a diagnostic (exit code 1, no mutation) if the database is
not at the expected 9-round baseline or a fully, correctly continued
20-round state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.audit import ActorContext
from app.db import connect
from app.migrations import migrate
from app.replay_continuation import (
    SOURCE_ROUND_COUNT,
    ReplayContinuationError,
    continue_second_half_regular_season,
    describe_second_half_continuation,
)

ACTOR = ActorContext.anonymous_operator("replay_operator")


def cmd_status(database, _args: argparse.Namespace) -> int:
    report = describe_second_half_continuation(database)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ready"] else 1


def cmd_continue(database, args: argparse.Namespace) -> int:
    report = continue_second_half_regular_season(database, actor=ACTOR, reason=args.reason)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["already_continued"]:
        print("Season was already continued to 20 rounds; no changes made (idempotent).", file=sys.stderr)
    else:
        print(
            f"Continued season {report['season_id']} from {SOURCE_ROUND_COUNT} to "
            f"{report['regular_season_round_count']} regular-season rounds. Round 10 is unopened; "
            "proceed with the normal Round Preflight workflow.",
            file=sys.stderr,
        )
    return 0


COMMANDS = {"status": cmd_status, "continue": cmd_continue}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="report readiness/continuation state; makes no writes")
    continue_parser = subparsers.add_parser("continue", help="perform (or idempotently confirm) the continuation")
    continue_parser.add_argument("--reason", help="audit-trail reason; a sensible default is used if omitted")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print(
            "Refusing to run the 2026 second-half replay continuation CLI while BBBFFL_ENVIRONMENT=production.",
            file=sys.stderr,
        )
        return 1

    # `status` is documented and parsed as a read-only probe -- it must never
    # upgrade the schema of a database it is only meant to inspect. Only
    # `continue` (which mutates the database on purpose) runs the migrator
    # first, exactly like scripts/replay_2026_midseason_draft.py does before
    # its own mutating commands.
    if args.command == "continue":
        migrate(args.database_url)
    database = connect(args.database_url)
    try:
        return COMMANDS[args.command](database, args)
    except ReplayContinuationError as exc:
        print(f"replay continuation refused: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
