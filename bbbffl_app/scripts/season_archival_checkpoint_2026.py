"""Operator CLI for issue #194's final archival-checkpoint guard.

This is deliberately the *only* mutating action this script can trigger:
none. `verify` is read-only, exactly like `finals_bracket_2026`'s preview
subcommands -- it never migrates, never locks, never writes. It exists so an
operator has one command, not an ad-hoc SQL query, to answer "has issue
#195's completion transaction actually committed, and what is the exact
completed-season version/completion-event identifier the final `pg_dump` +
checkpoint JSON must be labelled with?" before taking that archival backup.

See `docs/2026-finals-superscore-playbook.md` section on the final archival
checkpoint for the full operator procedure this command is step one of, and
`app/season_archival.py` for the verification this wraps.

Usage
-----

    cd bbbffl_app
    python -m scripts.season_archival_checkpoint_2026 \\
        --database-url postgresql://bbbffl:...@database/bbbffl_2026_finals \\
        verify --season-id <season_id>

    # Re-confirming against a completion-event id already recorded in the
    # provenance manifest (e.g. from a prior `verify` run, or printed by
    # whoever ran issue #195's `complete_season`):
    python -m scripts.season_archival_checkpoint_2026 \\
        --database-url ... verify --season-id <season_id> \\
        --expected-completion-event-id <event_id>

Exit code 0 and a JSON report on success; exit code 1 and a diagnostic on
stderr if the season is not `completed` yet, has no `season.completed`
audit event, or (with `--expected-completion-event-id`) the currently
observed completion event does not match. Never run this against
`BBBFFL_ENVIRONMENT=production`, matching every other replay operator CLI
in this repository -- this is a 2026 replay tool, not a live-season one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.db import connect
from app.season_archival import SeasonArchivalError, verify_season_completed_for_archival


def cmd_verify(database, args: argparse.Namespace) -> int:
    result = verify_season_completed_for_archival(
        database, args.season_id, expected_completion_event_id=args.expected_completion_event_id
    )
    payload = {
        "season_id": result.season_id,
        "completed_season_version": result.completed_season_version,
        "completion_event_id": result.completion_event_id,
        "completion_event_occurred_at": result.completion_event_occurred_at,
        "verified_at": result.verified_at,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(
        "Season is completed. Record completed_season_version and completion_event_id above in "
        "provenance-manifest.md's final archival checkpoint entry, THEN take the paired pg_dump/checkpoint "
        "backup -- never the other way around.",
        file=sys.stderr,
    )
    return 0


COMMANDS = {"verify": cmd_verify}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser(
        "verify", help="read-only check that the season is completed and report its completion identifier"
    )
    verify_parser.add_argument("--season-id", required=True)
    verify_parser.add_argument(
        "--expected-completion-event-id",
        default=None,
        help="optional: fail unless the currently observed season.completed event matches this exact event id",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print(
            "Refusing to run the 2026 finals/SuperScore archival-checkpoint CLI while BBBFFL_ENVIRONMENT=production.",
            file=sys.stderr,
        )
        return 1

    # This command never mutates -- it never runs the migrator, exactly like
    # every other read-only `preview`/`status` subcommand in this repository.
    database = connect(args.database_url)
    try:
        return COMMANDS[args.command](database, args)
    except SeasonArchivalError as exc:
        print(f"archival checkpoint refused: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
