"""Drive the 2026 mid-season draft (issue #164) through its real lifecycle,
one auditable Scorer/replay-operator action at a time -- continuing the
2026 historical replay after BBBFFL Round 10.

Unlike `scripts/replay_2026_draft.py`/`scripts/replay_2026_preseason.py`,
this script does not fabricate a whole illustrative scenario: it exposes
one subcommand per real `app.midseason_draft.MidseasonDraftRepository`
action, so an operator can apply each actual competition decision (which
players were delisted, which trades were agreed) as evidence becomes
available, exactly the way `scripts/first_half_replay.py`'s
acquire/validate/checkpoint subcommands drive round-by-round replay
progress. The one deliberately synthetic convenience is `auto-complete`,
which fills any remaining unresolved selections from the available pool
in canonical-id order -- only for exhausting a pick when no further
historical evidence exists, always logged as such, and never required if
every real selection is already known.

Every mutating action requires an existing, already-migrated database
(`--database-url`) holding a season whose BBBFFL rounds through its
configured `midseason_draft_trigger_round` are already final -- see
`scripts/bootstrap_2026_first_half.py` for how that season is normally
established.

Usage
-----

    cd bbbffl_app
    python -m scripts.replay_2026_midseason_draft confirm-ladder \\
        --database-url sqlite:///$(pwd)/data/2026-first-half.db \\
        --season-id <season_id> --competition-id <competition_id>
    python -m scripts.replay_2026_midseason_draft open-delisting-window ...
    python -m scripts.replay_2026_midseason_draft delist --season-entry-id ... --season-player-id ...
    python -m scripts.replay_2026_midseason_draft lock-delistings ...
    python -m scripts.replay_2026_midseason_draft generate-selections ...
    python -m scripts.replay_2026_midseason_draft pick --season-entry-id ... --season-player-id ...
    python -m scripts.replay_2026_midseason_draft auto-complete ...
    python -m scripts.replay_2026_midseason_draft close-post-draft-trading ...
"""

from __future__ import annotations

import argparse
import os
import sys

from app.audit import ActorContext
from app.db import connect
from app.midseason_draft import MidseasonDraftRepository, MidseasonDraftStateError
from app.migrations import migrate

ACTOR = ActorContext.anonymous_operator("replay_operator")


def _print_status(midseason: MidseasonDraftRepository, season_id: str) -> None:
    draft = midseason.get_draft(season_id)
    if draft is None:
        print("No mid-season draft exists yet for this season.")
        return
    print(f"Mid-season draft state: {draft.state} (trigger round {draft.trigger_round_sequence})")
    status = midseason.status(season_id)
    if status is not None:
        print(f"  Selections: {status.completed_picks}/{status.total_picks} completed")


def cmd_confirm_ladder(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    draft = midseason.confirm_ladder(
        args.season_id, args.competition_id, actor=ACTOR, reason=args.reason or "2026 replay: ladder confirmed"
    )
    order = midseason.draft_order(args.season_id)
    print(f"Ladder confirmed; draft order (worst-placed picks first): {[e for _, e, _ in order]}")
    _print_status(midseason, draft.season_id)
    return 0


def cmd_override_order(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    if not args.reason:
        raise SystemExit("--reason is required for an audited draft-order override")
    midseason.override_draft_order(args.season_id, args.season_entry_id, actor=ACTOR, reason=args.reason)
    print("Draft order overridden.")
    return 0


def cmd_open_delisting_window(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    midseason.open_delisting_window(args.season_id, actor=ACTOR, reason=args.reason)
    print("Delisting window opened.")
    return 0


def cmd_delist(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    delisting = midseason.submit_delisting(
        args.season_id, args.season_entry_id, args.season_player_id, actor=ACTOR, reason=args.reason
    )
    print(f"Delisting recorded: {delisting.delisting_id}")
    return 0


def cmd_withdraw_delisting(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    midseason.withdraw_delisting(args.season_id, args.delisting_id, actor=ACTOR, reason=args.reason)
    print("Delisting withdrawn.")
    return 0


def cmd_trade(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    leg = {
        "leg_type": args.leg_type,
        "from_season_entry_id": args.from_entry,
        "to_season_entry_id": args.to_entry,
    }
    if args.leg_type == "player":
        leg["season_player_id"] = args.season_player_id
    else:
        leg["draft_round"] = args.draft_round
    trade = midseason.propose_trade(args.season_id, [leg], actor=ACTOR, reason=args.reason)
    print(f"Trade proposed: {trade.trade_id} (status={trade.status})")
    return 0


def cmd_decide_trade(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    trade = midseason.decide_trade(args.season_id, args.trade_id, args.approve, actor=ACTOR, reason=args.reason)
    print(f"Trade {trade.trade_id} is now {trade.status}.")
    return 0


def cmd_lock_delistings(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    midseason.lock_delistings(args.season_id, actor=ACTOR, reason=args.reason or "2026 replay: delistings locked")
    print("Delistings locked; delisted players released into the available pool.")
    return 0


def cmd_generate_selections(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    midseason.generate_selection_table(
        args.season_id, actor=ACTOR, reason=args.reason or "2026 replay: selection table generated"
    )
    picks = midseason.picks(args.season_id)
    print(f"Selection table generated: {len(picks)} picks across {picks[-1].draft_round if picks else 0} rounds.")
    return 0


def cmd_pick(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    pick = midseason.execute_pick(
        args.season_id,
        args.season_entry_id,
        args.season_player_id,
        actor=ACTOR,
        reason=args.reason or "2026 replay: mid-season selection",
    )
    print(f"Pick {pick.overall_number} completed.")
    _print_status(midseason, args.season_id)
    return 0


def cmd_auto_complete(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    """Fill any remaining unresolved selections from the available pool, in
    canonical-id order -- a deliberately synthetic convenience for
    exhausting a pick only once no further historical evidence exists.
    Never used to override a known, evidenced historical selection."""
    completed = 0
    while True:
        pick = midseason.next_pick(args.season_id)
        if pick is None:
            break
        pool = sorted(midseason.available_player_pool(args.season_id), key=lambda item: item.canonical_player_id)
        if not pool:
            raise SystemExit("no eligible available players remain to auto-complete the draft")
        midseason.execute_pick(
            args.season_id,
            pick.current_season_entry_id,
            pool[0].season_player_id,
            actor=ACTOR,
            reason="2026 replay: auto-complete (SIMULATION -- no further historical evidence)",
        )
        completed += 1
    print(f"Auto-completed {completed} remaining selection(s).")
    _print_status(midseason, args.season_id)
    return 0


def cmd_close_post_draft_trading(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    midseason.close_post_draft_trading(
        args.season_id, actor=ACTOR, reason=args.reason or "2026 replay: post-draft trading closed for Round 11"
    )
    print("Post-draft trading closed; the season proceeds into Round 11.")
    return 0


def cmd_status(midseason: MidseasonDraftRepository, args: argparse.Namespace) -> int:
    _print_status(midseason, args.season_id)
    return 0


COMMANDS = {
    "confirm-ladder": cmd_confirm_ladder,
    "override-order": cmd_override_order,
    "open-delisting-window": cmd_open_delisting_window,
    "delist": cmd_delist,
    "withdraw-delisting": cmd_withdraw_delisting,
    "trade": cmd_trade,
    "decide-trade": cmd_decide_trade,
    "lock-delistings": cmd_lock_delistings,
    "generate-selections": cmd_generate_selections,
    "pick": cmd_pick,
    "auto-complete": cmd_auto_complete,
    "close-post-draft-trading": cmd_close_post_draft_trading,
    "status": cmd_status,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def base(name):
        sub = subparsers.add_parser(name)
        sub.add_argument("--season-id", required=True)
        return sub

    base("status")

    p = base("confirm-ladder")
    p.add_argument("--competition-id", required=True)
    p.add_argument("--reason")

    p = base("override-order")
    p.add_argument("--season-entry-id", nargs="+", required=True, help="the full new order, worst-picks-first")
    p.add_argument("--reason", required=True)

    p = base("open-delisting-window")
    p.add_argument("--reason")

    p = base("delist")
    p.add_argument("--season-entry-id", required=True)
    p.add_argument("--season-player-id", required=True)
    p.add_argument("--reason")

    p = base("withdraw-delisting")
    p.add_argument("--delisting-id", required=True)
    p.add_argument("--reason")

    p = base("trade")
    p.add_argument("--leg-type", choices=("player", "pick"), required=True)
    p.add_argument("--from-entry", required=True)
    p.add_argument("--to-entry", required=True)
    p.add_argument("--season-player-id")
    p.add_argument("--draft-round", type=int)
    p.add_argument("--reason")

    p = base("decide-trade")
    p.add_argument("--trade-id", required=True)
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", dest="approve", action="store_false")
    p.set_defaults(approve=True)
    p.add_argument("--reason")

    p = base("lock-delistings")
    p.add_argument("--reason")

    p = base("generate-selections")
    p.add_argument("--reason")

    p = base("pick")
    p.add_argument("--season-entry-id", required=True)
    p.add_argument("--season-player-id", required=True)
    p.add_argument("--reason")

    base("auto-complete")

    p = base("close-post-draft-trading")
    p.add_argument("--reason")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if (os.getenv("BBBFFL_ENVIRONMENT") or "").strip().lower() == "production":
        print("Refusing to run the mid-season draft replay CLI while BBBFFL_ENVIRONMENT=production.", file=sys.stderr)
        return 1

    migrate(args.database_url)
    database = connect(args.database_url)
    try:
        midseason = MidseasonDraftRepository(database)
        handler = COMMANDS[args.command]
        return handler(midseason, args)
    except MidseasonDraftStateError as exc:
        print(f"mid-season draft action refused: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
