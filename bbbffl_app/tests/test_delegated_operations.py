"""Route-orchestration regressions for issue #117's delegated surface."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.authorization import Principal, Role
from app.main import opening_round_error_handler
from app.opening_round import OpeningRoundError
from app.routes import delegated_operations


@pytest.mark.parametrize(
    "persisted_positions,visible_positions,persisted_revision",
    [
        ({"F1": "draft-a"}, {"F1": "draft-b"}, 7),
        ({}, {"F1": "first-visible-selection"}, 1),
    ],
)
def test_submit_saves_and_submits_exact_visible_positions(
    monkeypatch, persisted_positions, visible_positions, persisted_revision
):
    """Direct Submit must never authorise the older persisted draft."""
    calls = []
    resulting_draft = SimpleNamespace(lineup_id="lineup-1", revision=persisted_revision + 1)

    class Proxy:
        def __init__(self, database, afl_client):
            pass

        def create_or_amend(self, season_id, competition_id, round_id, entry_id, positions, **kwargs):
            calls.append(("save", dict(positions), kwargs["expected_revision"], kwargs["actor"]))
            return resulting_draft

        def submit(self, lineup_id, **kwargs):
            calls.append(("submit", lineup_id, kwargs["expected_draft_revision"], kwargs["actor"]))

    scope = {
        "season_id": "season-a",
        "competition_id": "competition-a",
        "bbbffl_round_id": "round-a",
        "season_entry_id": "entry-a",
    }
    monkeypatch.setattr(delegated_operations, "LineupProxyService", Proxy)
    monkeypatch.setattr(delegated_operations, "_scope", lambda *args: scope)
    monkeypatch.setattr(delegated_operations, "_csrf", lambda *args: None)
    monkeypatch.setattr(delegated_operations, "_guard", lambda *args: "guard")
    monkeypatch.setattr(delegated_operations, "_lineup_view", lambda *args: {"ok": True})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=object(), afl_client=object())))
    principal = Principal(Role.REPLAY_OPERATOR, coach_id="operator-1")
    payload = delegated_operations.SubmitRequest(
        positions=visible_positions,
        expected_draft_revision=persisted_revision,
        expected_submission_version=2,
        reason="reviewed visible selections",
    )

    assert delegated_operations.submit("round-a", payload, request, principal) == {"ok": True}
    assert calls[0][0:3] == ("save", visible_positions, persisted_revision)
    assert calls[1][0:3] == ("submit", "lineup-1", persisted_revision + 1)
    assert calls[0][1] != persisted_positions or visible_positions == persisted_positions
    assert calls[0][3].actor_id == "operator-1"
    assert calls[1][3].actor_id == "operator-1"


def test_opening_round_domain_conflict_has_controlled_http_409_response():
    response = asyncio.run(
        opening_round_error_handler(SimpleNamespace(), OpeningRoundError("target slot M1 is already nominated"))
    )
    assert response.status_code == 409
    assert json.loads(response.body) == {"detail": "target slot M1 is already nominated"}
