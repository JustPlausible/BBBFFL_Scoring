"""Unit tests for the app.scorer_decisions application-service layer.

These exercise the module directly against a real (SQLite) DecisionsRepository
-- no FastAPI/TestClient involved -- to prove the service functions extracted
from routes/admin.py and routes/superscore.py are usable outside an HTTP
request (issue #36: "keep application/domain services usable from HTTP
routes, admin workflows, deterministic replay and tests"). tests/test_api.py
and tests/test_superscore_api.py cover the same rules again through the full
HTTP surface.
"""

from dataclasses import dataclass

import pytest

from app.audit import ActorContext
from app.db import DecisionsRepository
from app.scorer_decisions import (
    ADMIN_ACTOR,
    SCORER_ACTOR,
    CompetitionFinalizedError,
    InvalidPositionError,
    InvalidSlotError,
    ResultNotReadyError,
    UnknownTeamError,
    finalize,
    set_dnp,
    set_interchange,
    set_override,
)
from tests.db_helpers import migrated_connection

TEAM_KEYS = {"team_a", "team_b"}


@pytest.fixture
def decisions():
    return DecisionsRepository(migrated_connection())


@dataclass(frozen=True)
class _FakeResult:
    """A minimal stand-in for service.MatchupResult/SuperScoreResult -- a
    real dataclass, since finalize() serialises it with dataclasses.asdict()
    exactly as routes/admin.py and routes/superscore.py do."""

    status: str
    teams: list = ()


def test_set_dnp_rejects_unknown_team(decisions):
    with pytest.raises(UnknownTeamError):
        set_dnp(decisions, TEAM_KEYS, "not_a_team", "Forward1", True)
    assert decisions.get_dnp_map() == {}


def test_set_dnp_rejects_unknown_slot(decisions):
    with pytest.raises(InvalidSlotError):
        set_dnp(decisions, TEAM_KEYS, "team_a", "not_a_slot", True)


def test_set_dnp_persists_through_the_repository(decisions):
    set_dnp(decisions, TEAM_KEYS, "team_a", "Forward1", True)
    assert decisions.get_dnp_map() == {("team_a", "Forward1"): True}


def test_set_interchange_rejects_unknown_position(decisions):
    with pytest.raises(InvalidPositionError):
        set_interchange(decisions, TEAM_KEYS, "team_a", "not_a_position")


def test_set_interchange_persists(decisions):
    set_interchange(decisions, TEAM_KEYS, "team_a", "Forward1")
    assignments = decisions.get_interchange_assignments()
    assert assignments["team_a"].target_position == "Forward1"


def test_set_override_rejects_unknown_position(decisions):
    with pytest.raises(InvalidPositionError):
        set_override(decisions, TEAM_KEYS, "team_a", "not_a_position", 10.0, "reason")


def test_set_override_persists(decisions):
    set_override(decisions, TEAM_KEYS, "team_a", "Forward1", 10.0, "scorer correction")
    overrides = decisions.get_overrides()
    assert overrides[("team_a", "Forward1")].override_score == 10.0


def test_decisions_are_locked_once_finalized(decisions):
    decisions.finalize("done", {"status": "FINAL", "teams": []})
    with pytest.raises(CompetitionFinalizedError):
        set_dnp(decisions, TEAM_KEYS, "team_a", "Forward1", True)
    with pytest.raises(CompetitionFinalizedError):
        set_interchange(decisions, TEAM_KEYS, "team_a", "Forward1")
    with pytest.raises(CompetitionFinalizedError):
        set_override(decisions, TEAM_KEYS, "team_a", "Forward1", 10.0, None)


def test_finalize_refuses_before_matches_complete(decisions):
    with pytest.raises(ResultNotReadyError):
        finalize(_FakeResult("LIVE"), decisions, "too early")
    assert decisions.get_matchup_state().finalized is False


def test_finalize_persists_the_supplied_result_as_the_frozen_snapshot(decisions):
    finalize(_FakeResult("AWAITING_SCORER_SIGNOFF"), decisions, "confirmed")
    state = decisions.get_matchup_state()
    assert state.finalized is True
    assert state.finalized_note == "confirmed"
    assert state.snapshot["status"] == "FINAL"


def test_actors_are_well_defined_non_impersonating_operators():
    """Extracted unchanged from routes/admin.py: scorer/admin duties are
    distinguished by actor_role even though there is one shared anonymous
    operator identity (see app/audit.py's actor convention)."""
    assert SCORER_ACTOR == ActorContext.anonymous_operator(role="scorer")
    assert ADMIN_ACTOR == ActorContext.anonymous_operator(role="admin")
