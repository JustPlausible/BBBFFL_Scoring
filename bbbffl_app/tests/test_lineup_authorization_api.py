import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.authorization import Principal, Role
from app.csrf import issue_token
from app.lineups import LineupConflictError
from app.main import lineup_conflict_handler
from app.routes.lineups import DraftRequest
from app.routes.round_review import _actor
from tests.test_competition_lifecycle import operational

GRAND_FINAL_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "grand_final_teams.json")


@pytest.fixture
def lineup_client(tmp_path, monkeypatch):
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{tmp_path / 'lineups.db'}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "operator-secret")
    monkeypatch.setenv("BBBFFL_TEAMS_CONFIG_PATH", GRAND_FINAL_CONFIG_PATH)
    monkeypatch.setenv("AFL_API_BASE_URL", "http://unused.invalid")
    from app.main import app

    with TestClient(app) as client:
        database = app.state.database
        lifecycle, round_, entries = operational(database, 9901, 9901)
        lifecycle.transition(round_.bbbffl_round_id, "open")
        scope = database.execute(
            "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
            "JOIN competition_stream c ON c.competition_id=r.competition_id "
            "WHERE r.bbbffl_round_id=?",
            (round_.bbbffl_round_id,),
        ).fetchone()
        coach = app.state.identities.get_current_coach(entries[0].season_entry_id)
        assert coach is not None
        issued = app.state.sessions.create(coach.coach_id, actor=ActorContext.coach(coach.coach_id))
        csrf = issue_token(app.state.settings.session_secret)
        client.cookies.set("bbbffl_session", issued.token)
        client.cookies.set("bbbffl_csrf", csrf)
        yield client, round_, entries, scope, csrf


def _url(round_, entry, scope):
    return (
        "/api/coach/lineups/draft"
        f"?season_id={scope['season_id']}&competition_id={scope['competition_id']}"
        f"&round_id={round_.bbbffl_round_id}&season_entry_id={entry.season_entry_id}"
    )


def _payload(round_, entry, scope, revision, positions):
    return {
        "season_id": scope["season_id"],
        "competition_id": scope["competition_id"],
        "round_id": round_.bbbffl_round_id,
        "season_entry_id": entry.season_entry_id,
        "expected_revision": revision,
        "positions": positions,
    }


def test_private_draft_schema_round_trips_unfilled_slots():
    payload = DraftRequest(
        season_id="season",
        competition_id="competition",
        round_id="round",
        season_entry_id="entry",
        expected_revision=1,
        positions={"F1": "player-1", "F2": None},
    )
    assert payload.positions == {"F1": "player-1", "F2": None}


def test_stale_private_draft_conflict_maps_to_http_409():
    response = asyncio.run(lineup_conflict_handler(SimpleNamespace(), LineupConflictError("stale draft revision")))
    assert response.status_code == 409
    assert response.body == b'{"detail":"stale draft revision"}'


def test_operator_provenance_role_comes_from_resolved_principal():
    actor = _actor(Principal(Role.SCORER), "Scorekeeper")
    assert actor.actor_id == "Scorekeeper"
    assert actor.actor_role == "scorer"


def test_private_draft_partial_round_trip_conflict_and_cross_coach_idor(lineup_client):
    client, round_, entries, scope, csrf = lineup_client
    owner, other = entries[:2]
    headers = {"X-CSRF-Token": csrf}

    created = client.put(
        _url(round_, owner, scope), json=_payload(round_, owner, scope, 0, {"F1": None}), headers=headers
    )
    assert created.status_code == 200, created.text
    assert created.json()["positions"]["F1"] is None

    read_back = client.get(_url(round_, owner, scope))
    assert read_back.status_code == 200
    assert read_back.json() == created.json()

    # A second stale tab loses the repository CAS and is an expected 409.
    first_edit = client.put(
        _url(round_, owner, scope), json=_payload(round_, owner, scope, 1, created.json()["positions"]), headers=headers
    )
    assert first_edit.status_code == 200
    stale = client.put(
        _url(round_, owner, scope), json=_payload(round_, owner, scope, 1, created.json()["positions"]), headers=headers
    )
    assert stale.status_code == 409

    before_other = client.app.state.lineups.get_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, other.season_entry_id
    )
    assert client.get(_url(round_, other, scope)).status_code == 404
    denied_write = client.put(
        _url(round_, other, scope), json=_payload(round_, other, scope, 0, {"F1": None}), headers=headers
    )
    assert denied_write.status_code == 404
    assert (
        client.app.state.lineups.get_draft(
            scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, other.season_entry_id
        )
        == before_other
    )
