"""scripts/superscore_round_2026.py (issue #194): argument parsing, the
production-environment guard, and a real ensure-stream/ensure-round/
confirm-mapping/setup-round/open-round/status round trip through the CLI's
own command handlers -- proving the operator entry point issue #194 added
for `app.superscore_round` (previously reachable only from tests) actually
works end to end, including against the PostgreSQL-only aggregate/FOR
UPDATE fix issue #194 made in that module."""

import argparse

import pytest

from app.audit import ActorContext
from app.superscore_round import ensure_round, ensure_stream, get_review_state
from scripts.superscore_round_2026 import (
    COMMANDS,
    build_parser,
    cmd_advance_to_review,
    cmd_confirm_mapping,
    cmd_ensure_round,
    cmd_ensure_stream,
    cmd_open_round,
    cmd_setup_round,
    cmd_status,
    main,
)


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


def _ready_superscore_stream_with_mapped_finals_week_1(year):
    """A finals bracket (week 1 accepted-mapped and opened) plus a sibling
    SuperScore stream under the same season -- the minimum `confirm-mapping`
    now requires, since it always derives the AFL mapping from the frozen
    `bbbffl_round_lifecycle` row `open_finals_week` creates for the round's
    own exact concurrent finals week (Codex review, PR #207, round 7: the
    mapping's own current head is not enough -- it must already be frozen
    onto that week's round)."""
    from app.finals import FinalsBracketRepository
    from app.finals_preflight import open_finals_week
    from tests.finals_helpers import accept_week_mapping, build_finals_ready_season
    from tests.superscore_helpers import FINALS_AFL_ROUNDS

    built = build_finals_ready_season(year=year)
    database, season = built["database"], built["season"]
    bracket = FinalsBracketRepository(database).create_bracket(
        season.season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ActorContext.anonymous_operator("test"),
        reason="confirm-mapping CLI regression test setup",
    )["bracket"]
    week1_round_id = database.execute(
        "SELECT bbbffl_round_id FROM finals_bracket_week WHERE bracket_id=? AND week_number=1", (bracket.bracket_id,)
    ).fetchone()["bbbffl_round_id"]
    accept_week_mapping(database, week1_round_id, year=season.year, afl_round_id=FINALS_AFL_ROUNDS[1])
    open_finals_week(database, bracket.bracket_id, 1, actor=ActorContext.anonymous_operator("test"))

    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
    ).fetchone()
    stream = ensure_stream(database, season.season_id, rules_row["rules_version_id"], built["ordinary_competition_id"])
    built["superscore_stream"] = stream
    return built


def test_cli_round_trip_creates_and_opens_a_superscore_round(monkeypatch):
    built = _ready_superscore_stream_with_mapped_finals_week_1(6001)
    database, season = built["database"], built["season"]
    stream = built["superscore_stream"]

    # ensure-stream is idempotent through the same CLI handler.
    stream_ns = argparse.Namespace(
        season_id=season.season_id,
        rules_version_id=database.execute(
            "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
        ).fetchone()["rules_version_id"],
        ordinary_competition_id=built["ordinary_competition_id"],
        reason="CLI regression test: stream setup",
    )
    assert cmd_ensure_stream(database, stream_ns) == 0

    round_ns = argparse.Namespace(competition_id=stream.competition_id, round_number=1)
    assert cmd_ensure_round(database, round_ns) == 0
    round_id = database.execute(
        "SELECT bbbffl_round_id FROM bbbffl_round WHERE competition_id=? AND round_key='ss1'",
        (stream.competition_id,),
    ).fetchone()["bbbffl_round_id"]

    from tests.superscore_helpers import FINALS_AFL_ROUNDS

    monkeypatch.setattr(
        "scripts.superscore_round_2026.ReplayAflDataSource",
        lambda *_a, **_k: _StubAflClient({(season.year, FINALS_AFL_ROUNDS[1])}),
    )
    mapping_ns = argparse.Namespace(
        round_id=round_id,
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

    advance_ns = argparse.Namespace(round_id=round_id, reason="CLI regression test: advance to review")
    assert cmd_advance_to_review(database, advance_ns) == 0
    from app.competition_lifecycle import CompetitionLifecycleRepository

    assert CompetitionLifecycleRepository(database).get_round(round_id).state == "review"


# -- confirm-mapping always derives from --round-id itself (Codex review, P1, three rounds) --


def test_confirm_mapping_derives_automatically_from_round_id(monkeypatch):
    """The CLI-wiring counterpart of `tests/test_superscore_round.py::
    test_resolves_the_matching_finals_week_s_accepted_mapping` -- the actual
    derivation logic is tested there; this only proves `cmd_confirm_mapping`
    reaches it correctly."""
    from app.round_mapping import RoundMappingRepository
    from tests.superscore_helpers import FINALS_AFL_ROUNDS

    built = _ready_superscore_stream_with_mapped_finals_week_1(6005)
    database, season = built["database"], built["season"]
    ss1_round_id = ensure_round(database, built["superscore_stream"].competition_id, 1, 1)

    monkeypatch.setattr(
        "scripts.superscore_round_2026.ReplayAflDataSource",
        lambda *_a, **_k: _StubAflClient({(season.year, FINALS_AFL_ROUNDS[1])}),
    )
    ns = argparse.Namespace(
        round_id=ss1_round_id,
        evidence_path="unused-under-stub.json",
        checkpoint_path=None,
        reason="CLI regression test: auto-derive from round-id itself",
    )
    assert cmd_confirm_mapping(database, ns) == 0
    mapping = RoundMappingRepository(database).resolve(ss1_round_id)
    assert (mapping.afl_season_id, mapping.afl_round_id) == (season.year, FINALS_AFL_ROUNDS[1])


def test_confirm_mapping_has_no_explicit_override_option():
    """Codex review, P1, round 3: a derived-but-overridable mapping left
    the override itself unchecked. Since every round this 2026-specific
    CLI handles genuinely has the finals-concurrency invariant, the fix is
    that there is no such option to misuse at all."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--database-url",
                "sqlite:///x.db",
                "confirm-mapping",
                "--round-id",
                "r1",
                "--afl-season-id",
                "2026",
                "--afl-round-id",
                "21",
                "--evidence-path",
                "x.json",
                "--reason",
                "x",
            ]
        )


# -- setup-round/open-round refuse a non-SuperScore round (Codex review, P2, round 4) --


def test_setup_round_refuses_a_finals_round(monkeypatch):
    from app.finals import FinalsBracketRepository
    from tests.finals_helpers import build_finals_ready_season

    built = build_finals_ready_season(year=6008)
    database, season = built["database"], built["season"]
    bracket = FinalsBracketRepository(database).create_bracket(
        season.season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ActorContext.anonymous_operator("test"),
        reason="setup-round-refuses-finals-round regression test setup",
    )["bracket"]
    week1_round_id = database.execute(
        "SELECT bbbffl_round_id FROM finals_bracket_week WHERE bracket_id=? AND week_number=1", (bracket.bracket_id,)
    ).fetchone()["bbbffl_round_id"]

    ns = argparse.Namespace(round_id=week1_round_id, reason="must refuse: not a SuperScore round")
    with pytest.raises(Exception, match="superscore"):
        cmd_setup_round(database, ns)
