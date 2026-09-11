"""Operator CLI for issue #187's 2026 replay-only finals-seeding snapshot.

Bridges the completed 2026 second-half replay's Round 20 home-and-away
season into the historical finals/SuperScore replay: the mathematical
Round 20 ladder (`app.ladder`) is read and reported, never mutated, and one
explicit, immutable, audited snapshot records the historical finals-seeding
order the 2026 competition actually used (`app.finals_seeding`). See that
module's docstring for the full domain rationale, including why this is
deliberately not a generic ladder editor and cannot be used against the
live 2027 season.

Usage
-----

`--database-url` is a top-level option -- like `git --git-dir=... status`,
it must come *before* the subcommand name, not after it:

    cd bbbffl_app
    python -m scripts.finals_seeding_2026 \\
        --database-url postgresql://bbbffl:...@database/bbbffl_2026_second_half \\
        preview --season-id <season_id> --competition-id <competition_id>
    python -m scripts.finals_seeding_2026 \\
        --database-url postgresql://bbbffl:...@database/bbbffl_2026_second_half \\
        apply --season-id <season_id> --competition-id <competition_id> \\
        --reason "2026 second-half replay: historical finals-seeding snapshot per issue #187"

`preview` never mutates. `apply` requires an explicit `--season-id`,
`--competition-id`, and substantive `--reason`; it is idempotent against an
already-applied snapshot with an unchanged resolution, and fails closed
(exit code 1, no mutation) outside the intended 2026 Round-20-complete
replay context, on an unresolvable historical team name, or against an
existing snapshot that no longer matches the freshly resolved seed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.audit import ActorContext
from app.db import connect
from app.finals_seeding import FinalsSeedingError, FinalsSeedingRepository
from app.migrations import migrate

ACTOR = ActorContext.anonymous_operator("replay_operator")


def cmd_preview(database, args: argparse.Namespace) -> int:
    report = FinalsSeedingRepository(database).preview(args.season_id, args.competition_id)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    # Not just `replay_context_ready`: an existing snapshot that no longer
    # matches the freshly resolved seed/competition also reports a
    # diagnostic and `apply_permitted: false` (apply would fail closed) --
    # that state must exit nonzero too, not read as validation success.
    return 0 if report["replay_context_ready"] and report["apply_permitted"] else 1


def cmd_apply(database, args: argparse.Namespace) -> int:
    repository = FinalsSeedingRepository(database)
    result = repository.apply(args.season_id, args.competition_id, actor=ACTOR, reason=args.reason)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if result["created"]:
        print(
            f"Created finals-seeding snapshot {result['snapshot_id']} for season {result['season_id']} "
            f"(audit event {result['audit_event_id']}).",
            file=sys.stderr,
        )
    else:
        print(
            f"A finals-seeding snapshot already exists for season {result['season_id']} "
            f"({result['snapshot_id']}) with an unchanged resolution; no changes made (idempotent).",
            file=sys.stderr,
        )
    return 0


COMMANDS = {"preview": cmd_preview, "apply": cmd_apply}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview_parser = subparsers.add_parser(
        "preview", help="read-only report of context/mathematical/historical order; makes no writes"
    )
    preview_parser.add_argument("--season-id", required=True)
    preview_parser.add_argument("--competition-id", required=True)

    apply_parser = subparsers.add_parser(
        "apply", help="create (or idempotently confirm) the historical finals-seeding snapshot"
    )
    apply_parser.add_argument("--season-id", required=True)
    apply_parser.add_argument("--competition-id", required=True)
    apply_parser.add_argument(
        "--reason", required=True, help="substantive audit-trail reason; no default is provided on purpose"
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print(
            "Refusing to run the 2026 finals-seeding replay CLI while BBBFFL_ENVIRONMENT=production.",
            file=sys.stderr,
        )
        return 1

    # `preview` is documented and parsed as a read-only probe -- it must
    # never upgrade the schema of a database it is only meant to inspect.
    # Only `apply` (which mutates the database on purpose) runs the migrator
    # first, exactly like scripts/replay_2026_second_half_continuation.py.
    if args.command == "apply":
        migrate(args.database_url)
    database = connect(args.database_url)
    try:
        return COMMANDS[args.command](database, args)
    except FinalsSeedingError as exc:
        print(f"finals-seeding refused: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
