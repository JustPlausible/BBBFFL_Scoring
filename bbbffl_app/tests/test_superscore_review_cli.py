"""scripts/superscore_review_2026.py (issue #194): argument parsing, the
production-environment guard, and a real dnp/interchange/override/status
round trip through the CLI's own command handlers -- proving the operator
entry point issue #194 added for `app.superscore_review.
SuperScoreReviewRepository` (previously reachable only from tests) actually
works end to end."""

import argparse

import pytest

from scripts.superscore_review_2026 import (
    COMMANDS,
    build_parser,
    cmd_dnp,
    cmd_interchange,
    cmd_override,
    cmd_status,
    main,
)
from tests.superscore_helpers import build_superscore_ready_season


def test_database_url_is_a_top_level_option():
    parser = build_parser()
    args = parser.parse_args(
        ["--database-url", "sqlite:///x.db", "status", "--round-id", "r1", "--season-entry-id", "e1"]
    )
    assert args.database_url == "sqlite:///x.db"
    assert args.command == "status"


def test_every_registered_command_has_a_handler():
    parser = build_parser()
    subparsers_action = next(
        action for action in parser._actions if getattr(action, "choices", None) and "status" in action.choices
    )
    assert set(subparsers_action.choices) == set(COMMANDS)


def test_mutating_subcommands_require_reason_and_expected_version():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--database-url",
                "sqlite:///x.db",
                "dnp",
                "--round-id",
                "r",
                "--season-entry-id",
                "e",
                "--slot",
                "F1",
                "--dnp",
                "true",
            ]
        )


def test_main_refuses_to_run_in_production(monkeypatch):
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "production")
    monkeypatch.setattr(
        "sys.argv",
        [
            "superscore_review_2026",
            "--database-url",
            "sqlite:///unused.db",
            "status",
            "--round-id",
            "r1",
            "--season-entry-id",
            "e1",
        ],
    )

    def _fail(*_args, **_kwargs):
        raise AssertionError("must not connect/migrate while BBBFFL_ENVIRONMENT=production")

    monkeypatch.setattr("scripts.superscore_review_2026.connect", _fail)
    monkeypatch.setattr("scripts.superscore_review_2026.migrate", _fail)
    assert main() == 1


def test_cli_round_trip_records_dnp_interchange_and_override():
    built = build_superscore_ready_season(year=6101)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    from app.superscore_round import open_round

    open_round(database, round_id, reason="open for CLI regression test")
    entry = built["entries"][0]

    # A real lineup submission is needed before a review-state row can be
    # ruled against -- `setup_round` only creates the row at review_version=0;
    # submission advances it to 1, exactly as tests/test_superscore_round.py
    # establishes.
    from app.lineups import WeeklyLineupRepository

    squad = built["ownership"].current_squad(entry.season_entry_id)
    lineups = WeeklyLineupRepository(database)
    draft = lineups.save_draft(
        built["season"].season_id,
        built["superscore_stream"].competition_id,
        round_id,
        entry.season_entry_id,
        {"F1": squad[0].season_player_id, "F2": squad[1].season_player_id},
        expected_revision=0,
    )
    lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)

    dnp_ns = argparse.Namespace(
        round_id=round_id,
        season_entry_id=entry.season_entry_id,
        slot="F1",
        dnp="true",
        expected_review_version=1,
        reason="CLI regression test: DNP",
    )
    assert cmd_dnp(database, dnp_ns) == 0

    interchange_ns = argparse.Namespace(
        round_id=round_id,
        season_entry_id=entry.season_entry_id,
        target_position="F1",
        expected_review_version=2,
        reason="CLI regression test: interchange",
    )
    assert cmd_interchange(database, interchange_ns) == 0

    override_ns = argparse.Namespace(
        round_id=round_id,
        season_entry_id=entry.season_entry_id,
        position="F2",
        override_score=12.5,
        calculated_score=4.0,
        expected_review_version=3,
        reason="CLI regression test: override",
    )
    assert cmd_override(database, override_ns) == 0

    status_ns = argparse.Namespace(round_id=round_id, season_entry_id=entry.season_entry_id)
    assert cmd_status(database, status_ns) == 0
