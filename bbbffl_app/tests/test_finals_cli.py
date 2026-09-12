"""scripts/finals_bracket_2026.py (issue #190): argument parsing, the
production-environment guard, and preview/apply against a real database
through the CLI's own command handlers -- mirrors
tests/test_finals_seeding_cli.py's shape for scripts/finals_seeding_2026.py."""

import argparse

import pytest

from app.finals import FinalsBracketRepository
from app.finals_preflight import build_finals_week_preflight
from scripts.finals_bracket_2026 import (
    build_parser,
    cmd_advance_apply,
    cmd_advance_preview,
    cmd_create_bracket_apply,
    cmd_create_bracket_preview,
    cmd_open_week,
    cmd_rewind,
    main,
)
from tests.finals_helpers import accept_week_mapping, build_finals_ready_season, seed_official_result


def test_database_url_is_a_top_level_option_documented_before_the_subcommand():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--database-url",
            "sqlite:///x.db",
            "create-bracket",
            "preview",
            "--season-id",
            "s1",
            "--competition-id",
            "c1",
            "--ordinary-competition-id",
            "o1",
        ]
    )
    assert args.database_url == "sqlite:///x.db"
    assert args.command == "create-bracket"
    assert args.mode == "preview"


def test_database_url_after_the_subcommand_is_rejected():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["create-bracket", "--database-url", "sqlite:///x.db", "preview"])


def test_create_bracket_apply_requires_a_reason_argument():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--database-url",
                "sqlite:///x.db",
                "create-bracket",
                "apply",
                "--season-id",
                "s1",
                "--competition-id",
                "c1",
                "--ordinary-competition-id",
                "o1",
            ]
        )


def test_advance_apply_requires_a_reason_argument():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--database-url", "sqlite:///x.db", "advance", "apply", "--bracket-id", "b1", "--from-week", "1"]
        )


def test_rewind_requires_a_reason_and_defaults_to_preview_only():
    parser = build_parser()
    args = parser.parse_args(
        ["--database-url", "sqlite:///x.db", "rewind", "--bracket-id", "b1", "--from-week", "1", "--reason", "r"]
    )
    assert args.apply is False


def test_main_refuses_to_run_in_production(monkeypatch):
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "production")
    monkeypatch.setattr(
        "sys.argv",
        [
            "finals_bracket_2026",
            "--database-url",
            "sqlite:///unused.db",
            "create-bracket",
            "preview",
            "--season-id",
            "s1",
            "--competition-id",
            "c1",
            "--ordinary-competition-id",
            "o1",
        ],
    )

    def _fail(*_args, **_kwargs):
        raise AssertionError("must not connect/migrate while BBBFFL_ENVIRONMENT=production")

    monkeypatch.setattr("scripts.finals_bracket_2026.connect", _fail)
    monkeypatch.setattr("scripts.finals_bracket_2026.migrate", _fail)

    assert main() == 1


def test_cli_preview_apply_round_trip_and_full_lifecycle_against_a_real_database():
    built = build_finals_ready_season(year=2500)
    database = built["database"]
    season_id = built["season"].season_id
    competition_id = built["finals_competition"].competition_id
    ordinary_id = built["ordinary_competition_id"]

    preview_ns = argparse.Namespace(
        season_id=season_id, competition_id=competition_id, ordinary_competition_id=ordinary_id
    )
    assert cmd_create_bracket_preview(database, preview_ns) == 0

    apply_ns = argparse.Namespace(
        season_id=season_id,
        competition_id=competition_id,
        ordinary_competition_id=ordinary_id,
        reason="CLI regression test",
    )
    assert cmd_create_bracket_apply(database, apply_ns) == 0
    # Idempotent repeat through the same CLI handler.
    assert cmd_create_bracket_apply(database, apply_ns) == 0

    bracket = FinalsBracketRepository(database).get_bracket(season_id, competition_id)
    for week in (1, 2, 3, 4):
        round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, week)
        accept_week_mapping(database, round_id, year=2500, afl_round_id=9500 + week)

    open_ns = argparse.Namespace(bracket_id=bracket.bracket_id, week=1)
    assert cmd_open_week(database, open_ns) == 0
    preflight = build_finals_week_preflight(database, bracket.bracket_id, 1)
    assert preflight["round_state"] == "open"

    pairings = {p.slot: p for p in FinalsBracketRepository(database).list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(database, pairings["qf"].matchup_id, 100, 50)
    seed_official_result(database, pairings["ef"].matchup_id, 50, 100)

    advance_preview_ns = argparse.Namespace(bracket_id=bracket.bracket_id, from_week=1)
    assert cmd_advance_preview(database, advance_preview_ns) == 0

    advance_apply_ns = argparse.Namespace(bracket_id=bracket.bracket_id, from_week=1, reason="CLI advance")
    assert cmd_advance_apply(database, advance_apply_ns) == 0

    week2 = FinalsBracketRepository(database).list_pairings(bracket.bracket_id, week_number=2)
    assert len(week2) == 2

    rewind_ns = argparse.Namespace(bracket_id=bracket.bracket_id, from_week=1, reason="CLI rewind preview", apply=False)
    assert cmd_rewind(database, rewind_ns) == 0


def test_cli_create_bracket_preview_reports_failure_exit_code_when_regular_season_incomplete():
    built = build_finals_ready_season(year=2501, trigger_round=19)
    namespace = argparse.Namespace(
        season_id=built["season"].season_id,
        competition_id=built["finals_competition"].competition_id,
        ordinary_competition_id=built["ordinary_competition_id"],
    )
    assert cmd_create_bracket_preview(built["database"], namespace) == 1
