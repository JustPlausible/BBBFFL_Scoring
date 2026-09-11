"""scripts/finals_seeding_2026.py (issue #187): argument parsing, the
production-environment guard, and preview/apply against a real database
through the CLI's own command handlers."""

import argparse

import pytest

from scripts.finals_seeding_2026 import COMMANDS, build_parser, cmd_apply, cmd_preview, main
from tests.finals_seeding_helpers import build_2026_replay_season


def test_database_url_is_a_top_level_option_documented_before_the_subcommand():
    parser = build_parser()
    args = parser.parse_args(
        ["--database-url", "sqlite:///x.db", "preview", "--season-id", "s1", "--competition-id", "c1"]
    )
    assert args.database_url == "sqlite:///x.db"
    assert args.command == "preview"
    assert args.season_id == "s1"
    assert args.competition_id == "c1"


def test_database_url_after_the_subcommand_is_rejected():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["preview", "--database-url", "sqlite:///x.db", "--season-id", "s1"])


def test_apply_requires_a_reason_argument():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--database-url", "sqlite:///x.db", "apply", "--season-id", "s1", "--competition-id", "c1"])


def test_every_registered_command_has_a_handler():
    parser = build_parser()
    subparsers_action = next(
        action for action in parser._actions if getattr(action, "choices", None) and "preview" in action.choices
    )
    assert set(subparsers_action.choices) == set(COMMANDS)


def test_main_refuses_to_run_in_production(monkeypatch):
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "production")
    monkeypatch.setattr(
        "sys.argv",
        [
            "finals_seeding_2026",
            "--database-url",
            "sqlite:///unused.db",
            "preview",
            "--season-id",
            "s1",
            "--competition-id",
            "c1",
        ],
    )

    def _fail(*_args, **_kwargs):
        raise AssertionError("must not connect/migrate while BBBFFL_ENVIRONMENT=production")

    monkeypatch.setattr("scripts.finals_seeding_2026.connect", _fail)
    monkeypatch.setattr("scripts.finals_seeding_2026.migrate", _fail)

    assert main() == 1


def test_cmd_preview_and_apply_round_trip_against_a_real_database():
    ctx = build_2026_replay_season()
    namespace = argparse.Namespace(season_id=ctx["season"].season_id, competition_id=ctx["competition"].competition_id)

    assert cmd_preview(ctx["database"], namespace) == 0

    apply_namespace = argparse.Namespace(
        season_id=ctx["season"].season_id,
        competition_id=ctx["competition"].competition_id,
        reason="issue #187 CLI regression test",
    )
    assert cmd_apply(ctx["database"], apply_namespace) == 0
    # Idempotent repeat through the same CLI handler.
    assert cmd_apply(ctx["database"], apply_namespace) == 0


def test_cmd_preview_reports_failure_exit_code_outside_replay_context():
    ctx = build_2026_replay_season(year=2027)
    namespace = argparse.Namespace(season_id=ctx["season"].season_id, competition_id=ctx["competition"].competition_id)
    assert cmd_preview(ctx["database"], namespace) == 1
