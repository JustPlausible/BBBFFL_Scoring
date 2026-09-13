"""Focused security/error translation coverage for Issue #193 routes."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.authorization import Principal, Role
from app.csrf import issue_token
from app.routes import superscore_results as routes
from app.superscore_results import (
    CompletedSeasonError,
    StaleSuperScoreEvidenceError,
    StaleSuperScorePublicationError,
    SuperScoreResultError,
)


class _Result:
    def fetchone(self):
        return {"bbbffl_round_id": "round", "season_id": "season"}


class _Database:
    def execute(self, *_args):
        return _Result()


class _Calculations:
    def __init__(self):
        self.calls = []

    def calculate_round(self, round_id):
        self.calls.append(round_id)
        return ["calculated"]


class _Service:
    def __init__(self, error=None):
        self.calculations = _Calculations()
        self.error = error
        self.actor = None

    def publish(self, _round_id, *, actor, reason):
        self.actor = actor
        if self.error:
            raise self.error
        return {"reason": reason}

    def leaderboard(self, _round_id, include_inputs=False):
        return {
            "kind": "superscore_leaderboard",
            "bbbffl_round_id": "round",
            "version": 2,
            "published_at": "2026-09-13T00:00:00Z",
            "published_by_type": "anonymous_operator",
            "published_by": "private-operator-id",
            "published_by_role": "scorer",
            "reason": "private correction details",
            "entries": [
                {
                    "season_entry_id": "entry",
                    "team_name": "Team",
                    "total_score": 123.0,
                    "rank": 1,
                    "is_joint_winner": False,
                }
            ],
        }

    def history(self, _round_id, include_inputs=False):
        return [self.leaderboard(_round_id, include_inputs)]


def _request(service, token=None):
    return SimpleNamespace(
        cookies={"bbbffl_csrf": token} if token else {},
        headers={"X-CSRF-Token": token} if token else {},
        app=SimpleNamespace(
            state=SimpleNamespace(
                database=_Database(), settings=SimpleNamespace(session_secret="secret"), superscore_results=service
            )
        ),
    )


@pytest.fixture(autouse=True)
def _season_scope(monkeypatch):
    monkeypatch.setattr(routes, "require_role_covers_season", lambda *_args: None)


def test_session_scorer_calculate_requires_valid_double_submit_csrf():
    principal = Principal(Role.SCORER, "coach-id", session_id="session-id")
    service = _Service()
    token = issue_token("secret")
    assert routes.calculate("round", _request(service, token), principal) == ["calculated"]
    assert service.calculations.calls == ["round"]

    for request in (_request(service), _request(service, "invalid")):
        with pytest.raises(HTTPException) as caught:
            routes.calculate("round", request, principal)
        assert caught.value.status_code == 403
        assert caught.value.detail == "Invalid CSRF token"


def test_header_token_operator_calculate_does_not_require_csrf():
    service = _Service()
    principal = Principal(Role.ADMIN)  # no session_id: established header-token path
    assert routes.calculate("round", _request(service), principal) == ["calculated"]


def test_public_leaderboard_allowlist_excludes_publication_audit_fields():
    result = routes.public_leaderboard("round", _request(_Service()))
    assert result == {
        "kind": "superscore_leaderboard",
        "bbbffl_round_id": "round",
        "version": 2,
        "published_at": "2026-09-13T00:00:00Z",
        "entries": [
            {
                "season_entry_id": "entry",
                "team_name": "Team",
                "total_score": 123.0,
                "rank": 1,
                "is_joint_winner": False,
            }
        ],
    }
    assert {"published_by", "published_by_role", "reason"}.isdisjoint(result)


def test_scorer_leaderboard_retains_publication_audit_fields():
    result = routes.scorer_leaderboard("round", _request(_Service()), Principal(Role.SCORER))
    assert result["current"]["published_by"] == "private-operator-id"
    assert result["current"]["published_by_role"] == "scorer"
    assert result["current"]["reason"] == "private correction details"
    assert result["history"][0]["reason"] == "private correction details"


def test_publication_is_a_privileged_operator_action_not_a_coach_action():
    service = _Service()
    principal = Principal(Role.REPLAY_OPERATOR, "operator-coach-id", session_id="session")
    token = issue_token("secret")
    routes.publish("round", routes.PublishRequest(reason="correction"), _request(service, token), principal)
    assert service.actor.actor_type == "anonymous_operator"
    assert service.actor.actor_id == "operator-coach-id"
    assert service.actor.actor_role == "replay_operator"

    token_service = _Service()
    routes.publish("round", routes.PublishRequest(), _request(token_service), Principal(Role.ADMIN))
    assert token_service.actor.actor_type == "anonymous_operator"
    assert token_service.actor.actor_id is None
    assert token_service.actor.actor_role == "admin"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (SuperScoreResultError("resolve DNP ruling for F1"), 409),
        (StaleSuperScorePublicationError("calculation changed; retry"), 409),
        (StaleSuperScoreEvidenceError("AFL evidence batch was stale"), 503),
        (CompletedSeasonError("completed seasons are immutable"), 423),
    ],
)
def test_expected_publication_refusals_have_actionable_http_responses(error, status):
    principal = Principal(Role.ADMIN)
    with pytest.raises(HTTPException) as caught:
        routes.publish("round", routes.PublishRequest(), _request(_Service(error)), principal)
    assert caught.value.status_code == status
    assert caught.value.detail == str(error)
