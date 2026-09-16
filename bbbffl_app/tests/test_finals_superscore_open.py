"""Issue #211 workflow improvement B: `app.finals_superscore_open.
open_finals_and_superscore_week` -- the single paired web "Open week"
action -- and its HTTP surface
(`POST /api/admin/finals/{bracket_id}/weeks/{week_number}/open-paired`).

Coverage proves the paired action:

- validates the pairing (an unmapped/blocked finals week, or a week with no
  configured SuperScore round) fails closed and mutates nothing;
- opens both the finals week and its concurrent SuperScore round, each
  through its own existing lifecycle transition, each recording its own
  separate audit event;
- synchronises SS's lockout plan from the now-opened finals week as part of
  the same action (issue #211 workflow A), so no separate manual step is
  required;
- is idempotent against a pairing that is already (partially) open.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.finals import FinalsBracketRepository
from app.finals_superscore_open import PairedOpenWeekError, open_finals_and_superscore_week
from app.lockouts import LockoutTriggerRepository
from app.superscore_round import ensure_round, ensure_stream
from tests.finals_helpers import accept_week_mapping
from tests.finals_seeding_helpers import build_2026_replay_season

ACTOR = ActorContext.anonymous_operator("test")


class _StubRound:
    def __init__(self, round_id):
        self.round_id = round_id


class _StubAflClient:
    """`round_exists` (used by `AflApiReferenceValidator`, which
    `synchronise_lockout_plan_from_finals` builds internally) must resolve
    the exact `afl_round_id` `_seed` maps the finals week onto -- an empty
    `get_rounds()` would otherwise make every SS mapping confirmation fail
    with "AFL season/round reference does not exist", even though the
    finals week's own mapping was already accepted through a real
    validator (`tests.finals_helpers.accept_week_mapping`)."""

    def __init__(self, afl_round_id=8801):
        self._afl_round_id = afl_round_id

    def get_matches(self, afl_round_id):
        return []

    def get_rounds(self, afl_season_id):
        return [_StubRound(self._afl_round_id)]


def _seed(database, year, *, with_ss1=True, with_finals_mapping=True, with_lockout_triggers=True):
    built = build_2026_replay_season(database=database, year=year)
    from app.season import SeasonRepository

    seasons = SeasonRepository(database)
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (built["season"].season_id,)
    ).fetchone()
    finals_competition = seasons.create_competition(
        built["season"].season_id, rules_row["rules_version_id"], "finals", "Finals", "finals"
    )
    repo = FinalsBracketRepository(database)
    bracket = repo.create_bracket(
        built["season"].season_id,
        finals_competition.competition_id,
        built["competition"].competition_id,
        actor=ACTOR,
        reason="issue #211 paired-open test bracket",
    )["bracket"]
    week1_round_id = repo.get_week_round_id(bracket.bracket_id, 1)
    afl_round_id = 8801
    if with_finals_mapping:
        accept_week_mapping(database, week1_round_id, year=year, afl_round_id=afl_round_id)
        if with_lockout_triggers:
            LockoutTriggerRepository(database).configure(
                week1_round_id, "main", "main", 1, [9999], actor=ACTOR, reason="finals main lockout"
            )

    built["bracket"] = bracket
    built["week1_round_id"] = week1_round_id
    built["afl_round_id"] = afl_round_id

    if with_ss1:
        stream = ensure_stream(
            database, built["season"].season_id, rules_row["rules_version_id"], built["competition"].competition_id
        )
        built["ss1_round_id"] = ensure_round(database, stream.competition_id, 1, 1)
    return built


def test_paired_open_fails_closed_when_no_superscore_round_is_configured():
    built = _seed(_database_for_test(9500), 9500, with_ss1=False)
    with pytest.raises(PairedOpenWeekError, match="no SuperScore round"):
        open_finals_and_superscore_week(
            built["database"], _StubAflClient(), built["bracket"].bracket_id, 1, actor=ACTOR
        )
    lifecycle = CompetitionLifecycleRepository(built["database"])
    assert lifecycle.get_round(built["week1_round_id"]) is None


def test_paired_open_fails_closed_when_finals_preflight_is_blocked():
    built = _seed(_database_for_test(9501), 9501, with_finals_mapping=False)
    with pytest.raises(PairedOpenWeekError, match="not ready to open|failed preflight"):
        open_finals_and_superscore_week(
            built["database"], _StubAflClient(), built["bracket"].bracket_id, 1, actor=ACTOR
        )
    lifecycle = CompetitionLifecycleRepository(built["database"])
    assert lifecycle.get_round(built["week1_round_id"]) is None
    assert lifecycle.get_round(built["ss1_round_id"]) is None


def test_paired_open_opens_both_streams_and_synchronises_the_ss_lockout_plan():
    built = _seed(_database_for_test(9502), 9502)
    result = open_finals_and_superscore_week(
        built["database"], _StubAflClient(), built["bracket"].bracket_id, 1, actor=ACTOR
    )
    assert result["finals_state"] == "open"
    assert result["superscore_state"] == "open"
    assert result["finals_already_open"] is False
    assert result["superscore_already_open"] is False
    assert result["lockout_sync"]["synced_trigger_keys"] == ["main"]

    ss_triggers = LockoutTriggerRepository(built["database"]).list_triggers(built["ss1_round_id"])
    assert [t.trigger_key for t in ss_triggers] == ["main"]

    lifecycle = CompetitionLifecycleRepository(built["database"])
    finals_audit = (
        built["database"]
        .execute("SELECT COUNT(*) AS n FROM audit_event WHERE entity_id=?", (built["week1_round_id"],))
        .fetchone()["n"]
    )
    ss_audit = (
        built["database"]
        .execute("SELECT COUNT(*) AS n FROM audit_event WHERE entity_id=?", (built["ss1_round_id"],))
        .fetchone()["n"]
    )
    assert finals_audit > 0
    assert ss_audit > 0
    assert lifecycle.get_round(built["week1_round_id"]).state == "open"
    assert lifecycle.get_round(built["ss1_round_id"]).state == "open"


def test_paired_open_is_idempotent_once_both_streams_are_already_open():
    built = _seed(_database_for_test(9503), 9503)
    open_finals_and_superscore_week(built["database"], _StubAflClient(), built["bracket"].bracket_id, 1, actor=ACTOR)

    result = open_finals_and_superscore_week(
        built["database"], _StubAflClient(), built["bracket"].bracket_id, 1, actor=ACTOR
    )
    assert result["finals_already_open"] is True
    assert result["superscore_already_open"] is True
    assert result["finals_state"] == "open"
    assert result["superscore_state"] == "open"


@pytest.fixture
def finals_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def test_open_paired_route_end_to_end_opens_both_streams(finals_client):
    built = _seed(finals_client.app.state.database, 9504)
    finals_client.app.state.afl_client = _StubAflClient(built["afl_round_id"])
    response = finals_client.post(f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/open-paired")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["finals_state"] == "open"
    assert body["superscore_state"] == "open"
    assert body["superscore_round_id"] == built["ss1_round_id"]


def test_open_paired_route_409s_without_a_superscore_round(finals_client):
    built = _seed(finals_client.app.state.database, 9505, with_ss1=False)
    response = finals_client.post(f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/open-paired")
    assert response.status_code == 409


def _database_for_test(year):
    """A standalone SQLite database for the pure service-level tests above
    (no TestClient/app needed) -- mirrors `tests.db_helpers.migrated_
    connection`'s shape but through the same `app.db.connect` path
    `tests.finals_seeding_helpers.build_2026_replay_season` expects."""
    from tests.db_helpers import migrated_connection

    return migrated_connection()
