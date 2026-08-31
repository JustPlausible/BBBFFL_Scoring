"""Seed an isolated, synthetic 2026 preseason-draft replay season for
interactively exercising the scorer-operated draft workflow (roadmap
package 14, issue #53) against the real admin UI.

Deprecated for historical first-half bootstrap: operators must use
``scripts.bootstrap_2026_first_half`` with explicit replay configuration and
captured afl-api data. This file remains demo/test scaffolding only.

This is a software simulation only -- it is NOT part of the hermetic test
suite, is never run by CI or plain `pytest`, and its generated player pool
is NOT real 2026 AFL player data (BBBFFL has no live afl-api access to a
2026 player pool from this environment -- see docs/afl-evidence-fixtures.md's
"Live validation status"). Do not represent any draft run through this
script as historical BBBFFL evidence.

Isolation
---------

Every run creates a brand-new season (a fresh UUID, never reused), so
running this script repeatedly against the same database never collides
with a prior replay run. By default it targets its own SQLite file
(`data/replay-2026-draft.db`), distinct from the application's normal
development database (`data/scorer_decisions.db`) and from any configured
production `BBBFFL_DATABASE_URL` -- pass `--database-url` explicitly to
target a different (e.g. shared PostgreSQL) database, but never point this
at a production database: it refuses outright when `BBBFFL_ENVIRONMENT`
is `production`.

Usage
-----

    cd bbbffl_app
    python -m scripts.replay_2026_draft --entries 10 --squad-limit 4

Then start the app against the same database and open the printed
`/admin/draft/<season_id>` URL:

    BBBFFL_DATABASE_URL=sqlite:///$(pwd)/data/replay-2026-draft.db \\
        uvicorn app.main:app --reload
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import uuid4

from app.config import BASE_DIR
from app.db import connect
from app.draft import DraftRepository
from app.identity import IdentityRepository
from app.migrations import migrate
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.season import SeasonRepository

DEFAULT_DATABASE_PATH = BASE_DIR / "data" / "replay-2026-draft.db"

# A high, reserved canonical_player_id range that can never collide with a
# real afl-api canonical player ID, so this synthetic pool is unambiguous
# even if later loaded alongside real 2026 season data.
SYNTHETIC_CANONICAL_ID_BASE = 9_000_000

FIRST_NAMES = [
    "Jack",
    "Tom",
    "Sam",
    "Will",
    "Harry",
    "Charlie",
    "Ben",
    "Noah",
    "Ethan",
    "Riley",
    "Cooper",
    "Lachlan",
    "Max",
    "Oscar",
    "Liam",
    "Zac",
    "Josh",
    "Nathan",
    "Mitch",
    "Dylan",
]
LAST_NAMES = [
    "Smith",
    "Anderson",
    "Walker",
    "Mitchell",
    "Bennett",
    "Carter",
    "Hughes",
    "Reid",
    "Turner",
    "Wallace",
    "Coleman",
    "Fraser",
    "Grant",
    "Holt",
    "Irwin",
    "Jenkins",
    "Kerr",
    "Lyons",
    "Moss",
    "Nash",
]


def synthetic_player_name(index: int) -> str:
    first = FIRST_NAMES[index % len(FIRST_NAMES)]
    last = LAST_NAMES[(index // len(FIRST_NAMES)) % len(LAST_NAMES)]
    return f"{first} {last} (2026 Replay #{index + 1})"


def seed_replay_draft(
    database_url: str, *, entries: int, squad_limit: int, auto_complete: bool = False
) -> tuple[str, str]:
    """Seed an isolated draft. With `auto_complete`, also run every pick to
    completion and finalize the draft entirely in software (round-robin over
    the synthetic pool in draft order) -- roadmap package 15 (issue #54)'s
    preseason window can only open once a draft is finalized, and the 2026
    replay path (`docs/preseason-trades.md`) needs a finalized draft to
    hand off to `scripts/replay_2026_preseason.py` without requiring the
    admin board's manual picks."""
    migrate(database_url)
    database = connect(database_url)
    try:
        season = SeasonRepository(database).create_season(2026, f"2026 Draft Replay (SIMULATION) {uuid4().hex[:8]}")
        identities = IdentityRepository(database)
        season_entries = []
        for number in range(1, entries + 1):
            coach = identities.create_coach(f"Replay Coach {number}")
            season_entries.append(
                identities.create_entry(
                    season.season_id, f"replay-2026-{uuid4().hex[:8]}-{number}", coach.coach_id, f"Replay Team {number}"
                )
            )

        OwnershipRepository(database).configure_squad_limit(season.season_id, squad_limit)

        pool = PlayerPoolRepository(database)
        player_count = entries * squad_limit + max(10, entries)
        players = [
            pool.refresh_player(
                season.season_id,
                SYNTHETIC_CANONICAL_ID_BASE + index,
                synthetic_player_name(index),
                source_provider="bbbffl-2026-replay-simulation",
            )
            for index in range(player_count)
        ]

        draft = DraftRepository(database)
        draft_id = draft.accept_order(season.season_id, [entry.season_entry_id for entry in season_entries])

        if auto_complete:
            for _ in range(entries * squad_limit):
                pick = draft.next_pick(season.season_id)
                draft.execute_pick(
                    season.season_id,
                    pick.current_season_entry_id,
                    players[pick.overall_number - 1].season_player_id,
                    reason="2026 replay auto-complete (SIMULATION)",
                )
            draft.finalize(season.season_id, note="2026 replay auto-complete (SIMULATION)")

        return season.season_id, draft_id
    finally:
        database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--entries", type=int, default=10, help="number of BBBFFL entries (default: 10)")
    parser.add_argument("--squad-limit", type=int, default=4, help="picks per entry (default: 4)")
    parser.add_argument(
        "--database-url",
        default=os.getenv("BBBFFL_REPLAY_DATABASE_URL", f"sqlite:///{DEFAULT_DATABASE_PATH}"),
        help=f"defaults to an isolated SQLite file at {DEFAULT_DATABASE_PATH}, never the app's normal database",
    )
    parser.add_argument(
        "--auto-complete",
        action="store_true",
        help="run every pick and finalize the draft in software instead of leaving it for the admin board "
        "(needed before scripts/replay_2026_preseason.py can open the preseason window)",
    )
    args = parser.parse_args()

    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print("Refusing to seed a replay draft while BBBFFL_ENVIRONMENT=production.", file=sys.stderr)
        return 1
    if args.entries < 1 or args.squad_limit < 1:
        print("--entries and --squad-limit must both be positive.", file=sys.stderr)
        return 1

    Path(DEFAULT_DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    season_id, draft_id = seed_replay_draft(
        args.database_url, entries=args.entries, squad_limit=args.squad_limit, auto_complete=args.auto_complete
    )

    print("Seeded an isolated 2026 draft replay season (software simulation only).")
    print(f"  database_url: {args.database_url}")
    print(f"  season_id:    {season_id}")
    print(f"  draft_id:     {draft_id}")
    print(f"  entries:      {args.entries}, squad_limit: {args.squad_limit}")
    if args.auto_complete:
        print("  draft:        auto-completed and finalized")
        print()
        print("Continue into the preseason window (roadmap package 15, issue #54):")
        print(f"  python -m scripts.replay_2026_preseason --database-url {args.database_url} --season-id {season_id}")
    else:
        print()
        print("Start the app against this same database, then open the draft board:")
        print(f"  BBBFFL_DATABASE_URL={args.database_url} uvicorn app.main:app --reload")
        print(f"  http://localhost:8000/admin/draft/{season_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
