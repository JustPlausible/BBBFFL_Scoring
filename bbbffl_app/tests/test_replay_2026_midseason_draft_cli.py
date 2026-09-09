"""Argument-parsing regression for scripts/replay_2026_midseason_draft.py
(issue #164): Codex review flagged that the module's own documented Usage
examples placed `--database-url` after the subcommand name, which the
parser (a top-level option registered before `add_subparsers`) does not
accept -- `status --database-url x --season-id s` exits with code 2."""

import pytest

from scripts.replay_2026_midseason_draft import COMMANDS, build_parser


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
