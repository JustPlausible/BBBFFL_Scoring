"""scripts/season_archival_checkpoint_2026.py (issue #194): argument
parsing, the production-environment guard, and that `verify` genuinely
refuses before completion and succeeds (reporting the exact identifiers)
after it -- the operator-facing wrapper around
`app.season_archival.verify_season_completed_for_archival`."""

import argparse

import pytest

from app.audit import ActorContext
from app.season_completion import complete_season
from scripts.season_archival_checkpoint_2026 import build_parser, cmd_verify, main
from tests.season_completion_helpers import build_completable_season

ACTOR = ActorContext.anonymous_operator("test")


def test_database_url_is_a_top_level_option():
    parser = build_parser()
    args = parser.parse_args(["--database-url", "sqlite:///x.db", "verify", "--season-id", "s1"])
    assert args.database_url == "sqlite:///x.db"
    assert args.command == "verify"
    assert args.expected_completion_event_id is None


def test_main_refuses_to_run_in_production(monkeypatch):
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "production")
    monkeypatch.setattr(
        "sys.argv",
        ["season_archival_checkpoint_2026", "--database-url", "sqlite:///unused.db", "verify", "--season-id", "s1"],
    )

    def _fail(*_args, **_kwargs):
        raise AssertionError("must not connect while BBBFFL_ENVIRONMENT=production")

    monkeypatch.setattr("scripts.season_archival_checkpoint_2026.connect", _fail)
    assert main() == 1


def test_cmd_verify_refuses_before_completion():
    built = build_completable_season(year=6401)
    namespace = argparse.Namespace(season_id=built["season"].season_id, expected_completion_event_id=None)
    with pytest.raises(Exception):
        cmd_verify(built["database"], namespace)


def test_cmd_verify_succeeds_and_reports_identifiers_after_completion(capsys):
    built = build_completable_season(year=6402)
    database, season_id = built["database"], built["season"].season_id
    result = complete_season(database, season_id, actor=ACTOR, reason="issue #194 archival CLI test")

    namespace = argparse.Namespace(season_id=season_id, expected_completion_event_id=None)
    assert cmd_verify(database, namespace) == 0
    printed = capsys.readouterr().out
    assert result.completion_event_id in printed
    assert str(result.completed_season_version) in printed
