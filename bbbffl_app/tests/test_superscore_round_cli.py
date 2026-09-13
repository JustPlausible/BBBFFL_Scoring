"""scripts/superscore_round_2026.py (issue #194): argument parsing, the
production-environment guard, and a real ensure-stream/ensure-round/
confirm-mapping/setup-round/open-round/status round trip through the CLI's
own command handlers -- proving the operator entry point issue #194 added
for `app.superscore_round` (previously reachable only from tests) actually
works end to end, including against the PostgreSQL-only aggregate/FOR
UPDATE fix issue #194 made in that module."""

import argparse

import pytest

from app.superscore_round import get_review_state
from scripts.superscore_round_2026 import (
    COMMANDS,
    build_parser,
    cmd_confirm_mapping,
    cmd_ensure_round,
    cmd_ensure_stream,
    cmd_open_round,
    cmd_setup_round,
    cmd_status,
    main,
)
from tests.finals_seeding_helpers import build_2026_replay_season


class _StubAflClient:
    """Duck-typed AFL client stand-in for `AflApiReferenceValidator` --
    avoids needing a real replay-evidence JSON file on disk just to prove
    this CLI's argument wiring reaches `confirm_afl_mapping` correctly (the
    underlying validation logic itself is already covered by
    tests/test_superscore_round.py and tests/test_round_mapping.py)."""

    def __init__(self, known):
        self._known = known

    def get_rounds(self, season_id):
        class _Round:
            def __init__(self, round_id):
                self.round_id = round_id

        return [_Round(round_id) for (s, round_id) in self._known if s == season_id]


def test_database_url_is_a_top_level_option():
    parser = build_parser()
    args = parser.parse_args(["--database-url", "sqlite:///x.db", "status", "--season-id", "s1", "--round-id", "r1"])
    assert args.database_url == "sqlite:///x.db"
    assert args.command == "status"


def test_every_registered_command_has_a_handler():
    parser = build_parser()
    subparsers_action = next(
        action for action in parser._actions if getattr(action, "choices", None) and "status" in action.choices
    )
    assert set(subparsers_action.choices) == set(COMMANDS)


def test_mutating_subcommands_require_reason():
    parser = build_parser()
    for command, extra in (
        ("ensure-stream", ["--season-id", "s", "--rules-version-id", "r", "--ordinary-competition-id", "o"]),
        ("setup-round", ["--round-id", "r"]),
        ("open-round", ["--round-id", "r"]),
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(["--database-url", "sqlite:///x.db", command, *extra])


def test_main_refuses_to_run_in_production(monkeypatch):
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "production")
    monkeypatch.setattr(
        "sys.argv",
        ["superscore_round_2026", "--database-url", "sqlite:///unused.db", "status", "--season-id", "s1"],
    )

    def _fail(*_args, **_kwargs):
        raise AssertionError("must not connect/migrate while BBBFFL_ENVIRONMENT=production")

    monkeypatch.setattr("scripts.superscore_round_2026.connect", _fail)
    monkeypatch.setattr("scripts.superscore_round_2026.migrate", _fail)
    assert main() == 1


def test_cli_round_trip_creates_and_opens_a_superscore_round(monkeypatch):
    built = build_2026_replay_season(year=6001)
    database, season = built["database"], built["season"]
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
    ).fetchone()

    stream_ns = argparse.Namespace(
        season_id=season.season_id,
        rules_version_id=rules_row["rules_version_id"],
        ordinary_competition_id=built["competition"].competition_id,
        reason="CLI regression test: stream setup",
    )
    assert cmd_ensure_stream(database, stream_ns) == 0
    # Idempotent repeat through the same CLI handler.
    assert cmd_ensure_stream(database, stream_ns) == 0

    stream = database.execute(
        "SELECT competition_id FROM superscore_stream WHERE season_id=?", (season.season_id,)
    ).fetchone()
    competition_id = stream["competition_id"]

    round_ns = argparse.Namespace(competition_id=competition_id, round_number=1)
    assert cmd_ensure_round(database, round_ns) == 0
    round_id = database.execute(
        "SELECT bbbffl_round_id FROM bbbffl_round WHERE competition_id=? AND round_key='ss1'", (competition_id,)
    ).fetchone()["bbbffl_round_id"]

    monkeypatch.setattr(
        "scripts.superscore_round_2026.ReplayAflDataSource",
        lambda *_a, **_k: _StubAflClient({(season.year, 21)}),
    )
    mapping_ns = argparse.Namespace(
        round_id=round_id,
        afl_season_id=season.year,
        afl_round_id=21,
        evidence_path="unused-under-stub.json",
        checkpoint_path=None,
        reason="CLI regression test: SS1 mapping",
    )
    assert cmd_confirm_mapping(database, mapping_ns) == 0

    setup_ns = argparse.Namespace(round_id=round_id, reason="CLI regression test: setup")
    assert cmd_setup_round(database, setup_ns) == 0
    # Idempotent repeat.
    assert cmd_setup_round(database, setup_ns) == 0

    status_ns = argparse.Namespace(season_id=season.season_id, round_id=round_id)
    assert cmd_status(database, status_ns) == 0

    open_ns = argparse.Namespace(round_id=round_id, reason="CLI regression test: open")
    assert cmd_open_round(database, open_ns) == 0

    for entry in built["entries"]:
        assert get_review_state(database, round_id, entry.season_entry_id) == 0
