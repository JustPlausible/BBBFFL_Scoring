"""Argument-parsing regression for scripts/replay_2026_midseason_draft.py
(issue #164): Codex review flagged that the module's own documented Usage
examples placed `--database-url` after the subcommand name, which the
parser (a top-level option registered before `add_subparsers`) does not
accept -- `status --database-url x --season-id s` exits with code 2."""

import argparse

import pytest

from scripts.replay_2026_midseason_draft import COMMANDS, build_parser, parse_trade_leg


def test_database_url_is_a_top_level_option_documented_before_the_subcommand():
    parser = build_parser()
    args = parser.parse_args(["--database-url", "sqlite:///x.db", "status", "--season-id", "season-1"])
    assert args.database_url == "sqlite:///x.db"
    assert args.command == "status"
    assert args.season_id == "season-1"


def test_database_url_after_the_subcommand_is_rejected():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["status", "--database-url", "sqlite:///x.db", "--season-id", "season-1"])


def test_every_registered_command_has_a_handler():
    parser = build_parser()
    subparsers_action = next(
        action for action in parser._actions if getattr(action, "choices", None) and "status" in action.choices
    )
    assert set(subparsers_action.choices) == set(COMMANDS)


def test_parse_trade_leg_accepts_player_and_pick_forms():
    assert parse_trade_leg("player:e1:e2:p1") == {
        "leg_type": "player",
        "from_season_entry_id": "e1",
        "to_season_entry_id": "e2",
        "season_player_id": "p1",
    }
    assert parse_trade_leg("pick:e1:e2:3") == {
        "leg_type": "pick",
        "from_season_entry_id": "e1",
        "to_season_entry_id": "e2",
        "draft_round": 3,
    }


@pytest.mark.parametrize(
    "spec",
    ["player:e1:e2", "pick:e1:e2:not-a-number", "swap:e1:e2:3", "garbage"],
)
def test_parse_trade_leg_rejects_malformed_specs(spec):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_trade_leg(spec)


def test_trade_command_accepts_multiple_repeatable_legs_for_a_player_for_pick_trade():
    """Codex review: a documented player-for-pick trade (or a same-round
    pick swap) needs more than one leg proposed atomically -- splitting it
    into separate `trade` invocations would create independently
    approvable/reversible trades, each covering only half of the actual
    agreement."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "--database-url",
            "sqlite:///x.db",
            "trade",
            "--season-id",
            "season-1",
            "--leg",
            "player:e1:e2:p1",
            "--leg",
            "pick:e2:e1:1",
            "--reason",
            "player-for-pick",
        ]
    )
    assert args.legs == [
        {
            "leg_type": "player",
            "from_season_entry_id": "e1",
            "to_season_entry_id": "e2",
            "season_player_id": "p1",
        },
        {
            "leg_type": "pick",
            "from_season_entry_id": "e2",
            "to_season_entry_id": "e1",
            "draft_round": 1,
        },
    ]
