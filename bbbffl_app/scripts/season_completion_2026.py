"""Operator CLI for issue #195's atomic `active -> completed` season
completion command (`app.season_completion`).

Issue #194 found `preview_complete_season`/`complete_season` had no
operator-reachable entry point -- no HTTP route, no CLI -- despite being
fully implemented and tested (`tests/test_season_completion*.py`). This
script adds exactly that surface, mirroring `scripts/finals_seeding_2026.py`'s
preview/apply shape, and changes nothing in `app.season_completion` itself:
this CLI performs steps 1-6 of "Checkpoint timing"
(`docs/2026-finals-superscore-design.md`) by calling the existing,
unmodified transaction -- it does not reimplement any part of it. Issue
#194's own step 7 (final archival evidence) is a *separate*, later, strictly
read-only action -- see `scripts/season_archival_checkpoint_2026.py` and
`docs/2026-finals-superscore-playbook.md` -- never folded into this command.

Usage
-----

    cd bbbffl_app
    python -m scripts.season_completion_2026 --database-url ... \\
        preview --season-id <season_id>
    python -m scripts.season_completion_2026 --database-url ... \\
        complete --season-id <season_id> \\
        --reason "2026 finals/SuperScore replay: season completion per issue #195"

`preview` never mutates. `complete` requires an explicit, substantive
`--reason`; it is NOT idempotently re-callable once it has succeeded (issue
#195's explicit scope: no reopen pathway) -- calling it again against an
already-`completed` season fails closed with no mutation. On success it
prints `completed_season_version`/`completion_event_id`, the exact
identifiers `scripts/season_archival_checkpoint_2026.py verify` re-derives
independently before any archival evidence may be taken.
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
from app.season_completion import SeasonCompletionError, complete_season, preview_complete_season

ACTOR = ActorContext.anonymous_operator("replay_operator")


def cmd_preview(database, args: argparse.Namespace) -> int:
    report = preview_complete_season(database, args.season_id)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["ready"] else 1


def cmd_complete(database, args: argparse.Namespace) -> int:
    result = complete_season(database, args.season_id, actor=ACTOR, reason=args.reason)
    payload = {
        "season_id": result.season.season_id,
        "lifecycle_state": result.season.lifecycle_state,
        "completed_season_version": result.completed_season_version,
        "completion_event_id": result.completion_event_id,
        "premiership_award_id": result.premiership_award.award_id,
        "premiership_season_entry_id": result.premiership_award.season_entry_id,
        "premiership_created": result.premiership_created,
        "wooden_spoon_award_id": result.wooden_spoon_award.award_id,
        "wooden_spoon_season_entry_id": result.wooden_spoon_award.season_entry_id,
        "wooden_spoon_created": result.wooden_spoon_created,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(
        f"Season {result.season.season_id} completed (version {result.completed_season_version}, "
        f"completion event {result.completion_event_id}). Record both identifiers in provenance-manifest.md's "
        "final archival checkpoint entry, then run 'scripts.season_archival_checkpoint_2026 verify' before "
        "taking the paired pg_dump/checkpoint backup.",
        file=sys.stderr,
    )
    return 0


COMMANDS = {"preview": cmd_preview, "complete": cmd_complete}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview_parser = subparsers.add_parser(
        "preview", help="read-only report of whether complete_season would currently succeed; never mutates"
    )
    preview_parser.add_argument("--season-id", required=True)

    complete_parser = subparsers.add_parser(
        "complete", help="run the atomic active -> completed season-completion transaction (steps 1-6)"
    )
    complete_parser.add_argument("--season-id", required=True)
    complete_parser.add_argument(
        "--reason", required=True, help="substantive audit-trail reason; no default is provided on purpose"
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print(
            "Refusing to run the 2026 season-completion operator CLI while BBBFFL_ENVIRONMENT=production.",
            file=sys.stderr,
        )
        return 1

    # Only `complete` mutates -- `preview` must never upgrade the schema of
    # a database it is only meant to inspect, exactly like every other
    # preview/apply CLI in this repository.
    if args.command == "complete":
        migrate(args.database_url)
    database = connect(args.database_url)
    try:
        return COMMANDS[args.command](database, args)
    except (SeasonCompletionError, SeasonCompletedError) as exc:
        print(f"season completion refused: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
