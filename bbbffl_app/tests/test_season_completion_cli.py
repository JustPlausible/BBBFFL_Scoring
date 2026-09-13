"""scripts/season_completion_2026.py (issue #194): argument parsing, the
production-environment guard, and a real preview/complete round trip
through the CLI's own command handlers -- proving the operator entry point
issue #194 added for `app.season_completion` (previously reachable only
from tests) actually works end to end, without reimplementing any part of
the underlying six-step transaction."""

import argparse

import pytest

from app.season import SeasonRepository
from scripts.season_completion_2026 import COMMANDS, build_parser, cmd_complete, cmd_preview, main
from tests.season_completion_helpers import build_completable_season


def test_database_url_is_a_top_level_option():
    parser = build_parser()
    args = parser.parse_args(["--database-url", "sqlite:///x.db", "preview", "--season-id", "s1"])
    assert args.database_url == "sqlite:///x.db"
    assert args.command == "preview"


def test_complete_requires_a_reason_argument():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--database-url", "sqlite:///x.db", "complete", "--season-id", "s1"])


def test_every_registered_command_has_a_handler():
    parser = build_parser()
    subparsers_action = next(
        action for action in parser._actions if getattr(action, "choices", None) and "preview" in action.choices
    )
    assert set(subparsers_action.choices) == set(COMMANDS)


def test_main_refuses_to_run_in_production(monkeypatch):
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "production")
    monkeypatch.setattr(
        "sys.argv", ["season_completion_2026", "--database-url", "sqlite:///unused.db", "preview", "--season-id", "s1"]
    )

    def _fail(*_args, **_kwargs):
        raise AssertionError("must not connect/migrate while BBBFFL_ENVIRONMENT=production")

    monkeypatch.setattr("scripts.season_completion_2026.connect", _fail)
    monkeypatch.setattr("scripts.season_completion_2026.migrate", _fail)
    assert main() == 1


def test_cmd_preview_reports_not_ready_before_the_season_qualifies():
    built = build_completable_season(year=6201)
    database = built["database"]
    # Undo the finals/SuperScore playout's effect on readiness by asking
    # about a season with no finals bracket at all yet.
    from tests.finals_seeding_helpers import build_2026_replay_season

    other = build_2026_replay_season(database=database, year=6202)
    namespace = argparse.Namespace(season_id=other["season"].season_id)
    assert cmd_preview(database, namespace) == 1


def test_cmd_preview_and_complete_round_trip_against_a_real_database():
    built = build_completable_season(year=6203)
    database, season_id = built["database"], built["season"].season_id

    assert cmd_preview(database, argparse.Namespace(season_id=season_id)) == 0

    complete_ns = argparse.Namespace(season_id=season_id, reason="issue #194 CLI regression test")
    assert cmd_complete(database, complete_ns) == 0

    season = SeasonRepository(database).get_season(season_id)
    assert season.lifecycle_state == "completed"

    # Not idempotently re-callable -- issue #195's explicit "no reopen
    # pathway" scope boundary, exercised through the CLI itself.
    with pytest.raises(Exception):
        cmd_complete(database, complete_ns)
