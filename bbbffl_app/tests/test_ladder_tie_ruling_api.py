"""Issue #241 HTTP/browser-flow coverage: the Ladder tie ruling page and
JSON API (`app.routes.ladder_tie_ruling`) driven through real HTTP requests
and real Scorer/Administrator/Coach sessions -- proves the wiring (role
boundary, season-scoped grants, CSRF, mandatory-reason refusal shape) works
end-to-end, the same way `tests/test_season_activation_api.py` proves
`app.routes.season_activation`'s wiring. Domain-level coverage (recording,
idempotency/supersede, staleness, the preview report shape) lives in
`tests/test_ladder_tie_ruling.py`; this file focuses on the browser surface
and authorization."""

import json
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext, AuditEventRepository
from app.authorization import Role
from tests.ladder_tie_ruling_helpers import build_tied_season

PASSWORD = "correct horse battery staple"  # noqa: S105 -- test fixture password, not a real credential


@pytest.fixture
def ruling_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _extract(pattern, html):
    match = re.search(pattern, html)
    assert match, pattern
    return match.group(1)


def _seed(client, year, **kwargs):
    built = build_tied_season(client.app.state.database, year=year, **kwargs)
    client.app.state.seasons.transition_lifecycle(
        built["season"].season_id, "active", actor=ActorContext.anonymous_operator("admin"), reason="activate"
    )
    return built


def _login(client, email):
    login_page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "email": email,
            "password": PASSWORD,
            "csrf_token": _extract(r'name="csrf_token" value="([^"]+)"', login_page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _role_session(client, role: Role, *, email, season_id=None):
    state = client.app.state
    coach = state.identities.create_coach(f"Test {role.value}", email=email)
    state.credentials.set_password(coach.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    state.role_grants.grant(
        coach.coach_id, role.value, season_id=season_id, actor=ActorContext.anonymous_operator("admin")
    )
    _login(client, email)
    account = client.get("/account")
    switch = client.post(
        "/api/context/role",
        json={"role": role.value},
        headers={"X-CSRF-Token": _extract(r'name="csrf_token" value="([^"]+)"', account.text)},
    )
    assert switch.status_code == 200, switch.text
    return coach.coach_id


def _plain_coach_session(client, *, email):
    state = client.app.state
    coach = state.identities.create_coach("Plain Coach", email=email)
    state.credentials.set_password(coach.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    _login(client, email)
    return coach.coach_id


def _ruling_csrf(client, season_id):
    page = client.get(f"/scorer/ladder-tie-ruling/{season_id}")
    assert page.status_code == 200
    assert "Ladder tie ruling" in page.text
    return {"X-CSRF-Token": json.loads(_extract(r"const csrf=(\"[^\"]+\")", page.text))}


def _audit_count(client):
    return len(AuditEventRepository(client.app.state.database).list_events(entity_type="ladder.tie_ruling"))


# -- Page and preview ---------------------------------------------------------


def test_ruling_page_is_reachable(ruling_client):
    client = ruling_client
    season_id = _seed(client, 8001)["season"].season_id
    page = client.get(f"/scorer/ladder-tie-ruling/{season_id}")
    assert page.status_code == 200
    assert "Ladder tie ruling" in page.text


def test_preview_reports_the_open_tie_and_team_names(ruling_client):
    client = ruling_client
    built = _seed(client, 8002)
    response = client.get(f"/api/scorer/ladder-tie-ruling/{built['season'].season_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["open_ties"]) == 1
    tie = body["open_ties"][0]
    assert tie["status"] == "unresolved"
    assert set(tie["tie_group"]) == set(built["tied_pair"])
    assert all(entry_id in body["team_names"] for entry_id in built["tied_pair"])


def test_unknown_season_is_404(ruling_client):
    client = ruling_client
    assert client.get("/api/scorer/ladder-tie-ruling/not-a-real-season").status_code == 404
    response = client.post(
        "/api/scorer/ladder-tie-ruling/not-a-real-season/rulings", json={"decided_order": ["a", "b"], "reason": "x"}
    )
    assert response.status_code == 404


# -- Successful recording -----------------------------------------------------


def test_ambient_operator_can_record_a_ruling(ruling_client):
    """The shared fixture's ambient "open operator" default (no
    `BBBFFL_ADMIN_TOKEN` configured) resolves to Administrator authority,
    exactly like every other admin-surface test in this suite."""
    client = ruling_client
    built = _seed(client, 8003)
    season_id = built["season"].season_id

    response = client.post(
        f"/api/scorer/ladder-tie-ruling/{season_id}/rulings",
        json={"decided_order": built["tied_pair"], "reason": "ambient operator ruling"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    assert body["ruling"]["decided_order"] == built["tied_pair"]
    assert body["preview"]["open_ties"][0]["status"] == "resolved"


def test_scorer_session_can_record_with_reason_and_csrf(ruling_client):
    client = ruling_client
    built = _seed(client, 8004)
    season_id = built["season"].season_id
    _role_session(client, Role.SCORER, email="scorer-8004@example.com", season_id=season_id)
    headers = _ruling_csrf(client, season_id)
    before_events = _audit_count(client)

    response = client.post(
        f"/api/scorer/ladder-tie-ruling/{season_id}/rulings",
        json={"decided_order": built["tied_pair"], "reason": "Scorer records the coin-toss result"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert _audit_count(client) == before_events + 1


def test_administrator_session_can_record(ruling_client):
    client = ruling_client
    built = _seed(client, 8005)
    season_id = built["season"].season_id
    _role_session(client, Role.ADMIN, email="admin-8005@example.com")
    headers = _ruling_csrf(client, season_id)

    response = client.post(
        f"/api/scorer/ladder-tie-ruling/{season_id}/rulings",
        json={"decided_order": built["tied_pair"], "reason": "Administrator records the ruling"},
        headers=headers,
    )
    assert response.status_code == 200, response.text


# -- Mandatory reason / invalid input -----------------------------------------


def test_missing_reason_is_a_400_and_records_nothing(ruling_client):
    client = ruling_client
    built = _seed(client, 8006)
    season_id = built["season"].season_id
    before = client.get(f"/api/scorer/ladder-tie-ruling/{season_id}").json()

    response = client.post(
        f"/api/scorer/ladder-tie-ruling/{season_id}/rulings", json={"decided_order": built["tied_pair"]}
    )

    assert response.status_code == 400, response.text
    after = client.get(f"/api/scorer/ladder-tie-ruling/{season_id}").json()
    assert after["open_ties"][0]["status"] == before["open_ties"][0]["status"] == "unresolved"


def test_a_tie_group_that_does_not_exist_is_a_409_and_records_nothing(ruling_client):
    client = ruling_client
    built = _seed(client, 8007)
    season_id = built["season"].season_id
    entries = client.app.state.identities.list_entries(season_id)
    not_tied = [entry.season_entry_id for entry in entries if entry.season_entry_id not in built["tied_pair"]][:2]

    response = client.post(
        f"/api/scorer/ladder-tie-ruling/{season_id}/rulings",
        json={"decided_order": not_tied, "reason": "these two are not actually tied"},
    )

    assert response.status_code == 409, response.text
    after = client.get(f"/api/scorer/ladder-tie-ruling/{season_id}").json()
    assert after["open_ties"][0]["status"] == "unresolved"


# -- Authorization: Scorer/Administrator only --------------------------------


def test_secretary_is_denied_recording_and_changes_nothing(ruling_client):
    client = ruling_client
    built = _seed(client, 8008)
    season_id = built["season"].season_id
    _role_session(client, Role.SECRETARY, email="secretary-8008@example.com", season_id=season_id)

    assert client.get(f"/api/scorer/ladder-tie-ruling/{season_id}").status_code == 403
    response = client.post(
        f"/api/scorer/ladder-tie-ruling/{season_id}/rulings",
        json={"decided_order": built["tied_pair"], "reason": "secretary tries"},
    )
    assert response.status_code == 403, response.text


def test_plain_coach_without_a_grant_is_denied(ruling_client):
    client = ruling_client
    built = _seed(client, 8009)
    season_id = built["season"].season_id
    _plain_coach_session(client, email="coach-8009@example.com")

    assert client.get(f"/api/scorer/ladder-tie-ruling/{season_id}").status_code == 403
    response = client.post(
        f"/api/scorer/ladder-tie-ruling/{season_id}/rulings",
        json={"decided_order": built["tied_pair"], "reason": "coach tries"},
    )
    assert response.status_code == 403, response.text


def test_unauthenticated_spectator_is_denied(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "a-real-configured-token")
    monkeypatch.setenv("BBBFFL_SESSION_SECRET", "a-real-configured-session-secret")
    from app.main import app

    try:
        with TestClient(app) as client:
            built = build_tied_season(client.app.state.database, year=8010)
            season_id = built["season"].season_id
            assert client.get(f"/api/scorer/ladder-tie-ruling/{season_id}").status_code == 401
            response = client.post(
                f"/api/scorer/ladder-tie-ruling/{season_id}/rulings",
                json={"decided_order": built["tied_pair"], "reason": "spectator tries"},
            )
            assert response.status_code == 401
    finally:
        db_path.unlink(missing_ok=True)


def test_scorer_grant_scoped_to_a_different_season_is_denied(ruling_client):
    client = ruling_client
    other_built = _seed(client, 8011)
    built = _seed(client, 8012)
    season_id = built["season"].season_id
    _role_session(
        client, Role.SCORER, email="scoped-scorer-8012@example.com", season_id=other_built["season"].season_id
    )

    assert client.get(f"/api/scorer/ladder-tie-ruling/{season_id}").status_code == 403
    response = client.post(
        f"/api/scorer/ladder-tie-ruling/{season_id}/rulings",
        json={"decided_order": built["tied_pair"], "reason": "wrong season"},
    )
    assert response.status_code == 403, response.text

    assert client.get(f"/api/scorer/ladder-tie-ruling/{other_built['season'].season_id}").status_code == 200


def test_session_write_without_csrf_token_is_refused(ruling_client):
    client = ruling_client
    built = _seed(client, 8013)
    season_id = built["season"].season_id
    _role_session(client, Role.SCORER, email="csrf-scorer-8013@example.com", season_id=season_id)

    response = client.post(
        f"/api/scorer/ladder-tie-ruling/{season_id}/rulings",
        json={"decided_order": built["tied_pair"], "reason": "no csrf"},
    )

    assert response.status_code == 403, response.text
