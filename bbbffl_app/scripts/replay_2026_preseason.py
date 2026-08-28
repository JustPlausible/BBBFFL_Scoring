"""Drive an already-finalized 2026 replay draft (see
`scripts/replay_2026_draft.py --auto-complete`) through the preseason
trade/finalisation window (roadmap package 15, issue #54): open the window,
apply one illustrative synthetic trade, validate every opening squad, and
close it -- printing the frozen opening squad for inspection.

This is a software simulation only -- it is NOT part of the hermetic test
suite, is never run by CI or plain `pytest`, and the trade it applies is a
deliberately labelled synthetic scenario, never a real league decision (see
`docs/roadmap/2027-season-roadmap.md`'s replay evidence-class convention).
Do not represent anything this script does as historical BBBFFL evidence.

Usage
-----

    cd bbbffl_app
    python -m scripts.replay_2026_draft --entries 10 --squad-limit 4 --auto-complete
    python -m scripts.replay_2026_preseason --database-url sqlite:///$(pwd)/data/replay-2026-draft.db \\
        --season-id <season_id printed above>
"""

from __future__ import annotations

import argparse
import os
import sys

from app.audit import ActorContext
from app.db import connect
from app.draft import DraftRepository
from app.identity import IdentityRepository
from app.migrations import migrate
from app.player_pool import OwnershipRepository
from app.preseason import PreseasonRepository, PreseasonSquadValidationError


def run_preseason_replay(database_url: str, season_id: str) -> None:
    migrate(database_url)
    database = connect(database_url)
    try:
        draft = DraftRepository(database)
        status = draft.status(season_id)
        if status is None:
            raise SystemExit(f"no draft found for season {season_id}")
        if not status.is_finalized:
            raise SystemExit(
                "the season's draft is not finalized -- run `python -m scripts.replay_2026_draft --auto-complete` first"
            )

        preseason = PreseasonRepository(database)
        ownership = OwnershipRepository(database)
        identities = IdentityRepository(database)
        actor = ActorContext.anonymous_operator("2026-replay-simulation")

        window = preseason.open_window(season_id, actor=actor, reason="2026 replay: preseason window opened")
        print(f"Preseason window opened at {window.opened_at}.")

        order = draft.order(season_id)
        if len(order) >= 2:
            entry_a, entry_b = order[0][1], order[1][1]
            squad_a = [period.season_player_id for period in ownership.squad_at(entry_a, "9999-01-01")]
            squad_b = [period.season_player_id for period in ownership.squad_at(entry_b, "9999-01-01")]
            if squad_a and squad_b:
                trade = preseason.submit_trade(
                    season_id,
                    [
                        {
                            "season_player_id": squad_a[0],
                            "from_season_entry_id": entry_a,
                            "to_season_entry_id": entry_b,
                        },
                        {
                            "season_player_id": squad_b[0],
                            "from_season_entry_id": entry_b,
                            "to_season_entry_id": entry_a,
                        },
                    ],
                    actor=actor,
                    reason="2026 replay: synthetic illustrative trade (SIMULATION)",
                )
                print(f"Applied one synthetic two-club trade ({trade.trade_id}).")

        issues = preseason.validate_squads(season_id)
        if issues:
            print("Opening squads are not all valid yet:")
            for issue in issues:
                print(f"  {issue}")
            raise SystemExit(1)

        closed = preseason.close_window(season_id, actor=actor, reason="2026 replay: preseason window closed")
        print(f"Preseason window closed at {closed.closed_at}. Opening squads frozen:")
        for position, entry_id in order:
            team = identities.get_public_team(entry_id)
            entries = preseason.opening_squad(season_id, entry_id)
            print(f"  {position}. {team.team_name if team else entry_id}: {len(entries)} players")
    finally:
        database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season-id", required=True, help="season_id printed by scripts.replay_2026_draft")
    parser.add_argument(
        "--database-url",
        required=True,
        help="must point at the same database scripts.replay_2026_draft seeded",
    )
    args = parser.parse_args()

    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print("Refusing to run the preseason replay while BBBFFL_ENVIRONMENT=production.", file=sys.stderr)
        return 1

    try:
        run_preseason_replay(args.database_url, args.season_id)
    except PreseasonSquadValidationError as exc:
        print(f"Preseason window could not be closed: {exc}", file=sys.stderr)
        for issue in exc.issues:
            print(f"  {issue}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
