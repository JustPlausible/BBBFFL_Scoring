"""Issue #239 HTTP/browser-flow coverage: the Season activation page and
JSON API (`app.routes.season_activation`) driven through real HTTP requests
and real Scorer/Administrator/Secretary/Coach sessions -- proves the wiring
(role boundary, season-scoped grants, CSRF, refusal shape) works end-to-end,
the same way `tests/test_season_setup_api.py` proves `app.routes.
season_setup`'s wiring rather than only `app.season_setup`'s own functions.
Domain/service-level readiness and mutation coverage lives in
`tests/test_season_activation.py`; this file focuses on the browser surface
and, per issue #239, authorization -- Scorer/Administrator only -- with
explicit proof that a rejected attempt changes neither the season's
lifecycle state nor the audit trail."""

import json
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext, AuditEventRepository
from app.authorization import Role
from tests.season_activation_helpers import build_activation_ready_season

PASSWORD = "correct horse battery staple"  # noqa: S105 -- test fixture password, not a real credential


@pytest.fixture
def activation_client(monkeypatch):
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
    return build_activation_ready_season(client.app.state.database, year=year, **kwargs)


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
    """Log in as a coach identity holding `role`'s grant and activate it,
    exactly as the account page does -- mirrors `tests/test_season_setup_
    api.py`'s `_scorer_session`, generalised across roles."""
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


def _activation_csrf(client, season_id):
    page = client.get(f"/scorer/season-activation/{season_id}")
    assert page.status_code == 200
    assert "Season activation" in page.text
    return {"X-CSRF-Token": json.loads(_extract(r"const csrf=(\"[^\"]+\")", page.text))}


def _lifecycle_state(client, season_id):
    return client.app.state.seasons.get_season(season_id).lifecycle_state


def _audit_count(client, season_id):
    return len(AuditEventRepository(client.app.state.database).list_events(entity_type="season", entity_id=season_id))


# -- Page and readiness preview -----------------------------------------------


def test_activation_page_is_reachable(activation_client):
    client = activation_client
    season_id = _seed(client, 7001)["season"].season_id
    page = client.get(f"/scorer/season-activation/{season_id}")
    assert page.status_code == 200
    assert "Season activation" in page.text


def test_readiness_preview_reports_ready_for_a_fully_prepared_season(activation_client):
    client = activation_client
    season_id = _seed(client, 7002)["season"].season_id
    response = client.get(f"/api/scorer/season-activation/{season_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready"] is True
    assert body["lifecycle_state"] == "setup"
    assert body["diagnostic"] is None
    assert len(body["checks"]) == 5
    assert all(check["ready"] for check in body["checks"])


def test_readiness_preview_names_blockers_for_an_unprepared_season(activation_client):
    client = activation_client
    season_id = _seed(
        client, 7003, create_entries=False, populate_pool=False, configure_squad=False, freeze_fixture=False
    )["season"].season_id
    response = client.get(f"/api/scorer/season-activation/{season_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready"] is False
    entries_check = next(c for c in body["checks"] if c["key"] == "entries")
    assert entries_check["ready"] is False
    assert "currently 0" in entries_check["detail"]


def test_unknown_season_is_404_for_both_readiness_and_activation(activation_client):
    client = activation_client
    assert client.get("/api/scorer/season-activation/not-a-real-season").status_code == 404
    response = client.post("/api/scorer/season-activation/not-a-real-season/activate", json={"reason": "test"})
    assert response.status_code == 404


# -- Successful activation ----------------------------------------------------


def test_ambient_operator_can_activate_a_ready_season(activation_client):
    """The shared fixture's ambient "open operator" default (no
    `BBBFFL_ADMIN_TOKEN` configured) resolves to Administrator authority,
    exactly like every other admin-surface test in this suite."""
    client = activation_client
    season_id = _seed(client, 7004)["season"].season_id

    response = client.post(f"/api/scorer/season-activation/{season_id}/activate", json={"reason": "ready to go"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["season"]["lifecycle_state"] == "active"
    assert body["previous_lifecycle_state"] == "setup"
    assert body["activation"]["ready"] is False
    assert body["activation"]["lifecycle_state"] == "active"
    assert _lifecycle_state(client, season_id) == "active"


def test_scorer_session_can_activate_with_reason_and_csrf(activation_client):
    client = activation_client
    season_id = _seed(client, 7005)["season"].season_id
    _role_session(client, Role.SCORER, email="scorer-7005@example.com", season_id=season_id)
    headers = _activation_csrf(client, season_id)
    before_events = _audit_count(client, season_id)

    response = client.post(
        f"/api/scorer/season-activation/{season_id}/activate",
        json={"reason": "Scorer confirms setup is complete"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert _lifecycle_state(client, season_id) == "active"
    assert _audit_count(client, season_id) == before_events + 1


def test_administrator_session_can_activate(activation_client):
    client = activation_client
    season_id = _seed(client, 7006)["season"].season_id
    _role_session(client, Role.ADMIN, email="admin-7006@example.com")
    headers = _activation_csrf(client, season_id)

    response = client.post(
        f"/api/scorer/season-activation/{season_id}/activate",
        json={"reason": "Administrator confirms setup is complete"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert _lifecycle_state(client, season_id) == "active"


# -- Missing prerequisites: 409, unchanged, no mutation audit event ---------


def test_missing_prerequisites_return_409_and_change_nothing(activation_client):
    client = activation_client
    season_id = _seed(client, 7007, finalize_draft=False, freeze_fixture=False)["season"].season_id
    before_events = _audit_count(client, season_id)

    response = client.post(f"/api/scorer/season-activation/{season_id}/activate", json={"reason": "too early"})

    assert response.status_code == 409, response.text
    assert "finalized" in response.json()["detail"]
    assert _lifecycle_state(client, season_id) == "setup"
    assert _audit_count(client, season_id) == before_events


def test_missing_reason_is_a_400_and_changes_nothing(activation_client):
    client = activation_client
    season_id = _seed(client, 7008)["season"].season_id
    before_events = _audit_count(client, season_id)

    response = client.post(f"/api/scorer/season-activation/{season_id}/activate", json={})

    assert response.status_code == 400, response.text
    assert _lifecycle_state(client, season_id) == "setup"
    assert _audit_count(client, season_id) == before_events


# -- Repeated activation: 409 with a clear current-state message ------------


def test_repeated_activation_returns_409_and_changes_nothing_further(activation_client):
    client = activation_client
    season_id = _seed(client, 7009)["season"].season_id
    first = client.post(f"/api/scorer/season-activation/{season_id}/activate", json={"reason": "first activation"})
    assert first.status_code == 200, first.text
    events_after_first = _audit_count(client, season_id)

    second = client.post(f"/api/scorer/season-activation/{season_id}/activate", json={"reason": "second attempt"})

    assert second.status_code == 409, second.text
    assert "already active" in second.json()["detail"]
    assert _lifecycle_state(client, season_id) == "active"
    assert _audit_count(client, season_id) == events_after_first


# -- Authorization: Scorer/Administrator only --------------------------------


def test_secretary_is_denied_activation_and_changes_nothing(activation_client):
    """Issue #239 explicitly scopes activation to Scorer/Administrator --
    unlike `app.routes.season_setup`'s `roundsetup.manage` capability
    (which also grants a Secretary), this surface must refuse a Secretary
    outright, even one holding a grant scoped to this exact season."""
    client = activation_client
    season_id = _seed(client, 7010)["season"].season_id
    _role_session(client, Role.SECRETARY, email="secretary-7010@example.com", season_id=season_id)
    before_events = _audit_count(client, season_id)

    assert client.get(f"/api/scorer/season-activation/{season_id}").status_code == 403
    response = client.post(f"/api/scorer/season-activation/{season_id}/activate", json={"reason": "secretary tries"})

    assert response.status_code == 403, response.text
    assert _lifecycle_state(client, season_id) == "setup"
    assert _audit_count(client, season_id) == before_events


def test_replay_operator_is_denied_activation(activation_client):
    """Also excluded, unlike several other Scorer-adjacent capabilities --
    issue #239 names only Scorer and Administrator."""
    client = activation_client
    season_id = _seed(client, 7011)["season"].season_id
    _role_session(client, Role.REPLAY_OPERATOR, email="replay-7011@example.com", season_id=season_id)

    assert client.get(f"/api/scorer/season-activation/{season_id}").status_code == 403
    response = client.post(f"/api/scorer/season-activation/{season_id}/activate", json={"reason": "replay operator"})
    assert response.status_code == 403, response.text
    assert _lifecycle_state(client, season_id) == "setup"


def test_plain_coach_without_a_grant_is_denied_and_changes_nothing(activation_client):
    client = activation_client
    season_id = _seed(client, 7012)["season"].season_id
    _plain_coach_session(client, email="coach-7012@example.com")
    before_events = _audit_count(client, season_id)

    assert client.get(f"/api/scorer/season-activation/{season_id}").status_code == 403
    response = client.post(f"/api/scorer/season-activation/{season_id}/activate", json={"reason": "coach tries"})

    assert response.status_code == 403, response.text
    assert _lifecycle_state(client, season_id) == "setup"
    assert _audit_count(client, season_id) == before_events


def test_unauthenticated_spectator_is_denied(monkeypatch):
    """The shared `activation_client` fixture deletes `BBBFFL_ADMIN_TOKEN`
    entirely (an "open operator" dev/test posture), so proving a genuinely
    unauthenticated request is rejected needs a configured token --
    matching `tests/test_admin_dashboard_api.py`'s established convention."""
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "a-real-configured-token")
    monkeypatch.setenv("BBBFFL_SESSION_SECRET", "a-real-configured-session-secret")
    from app.main import app

    try:
        with TestClient(app) as client:
            season_id = _seed(client, 7013)["season"].season_id
            response = client.get(f"/api/scorer/season-activation/{season_id}")
            assert response.status_code == 401
            mutation = client.post(
                f"/api/scorer/season-activation/{season_id}/activate", json={"reason": "spectator tries"}
            )
            assert mutation.status_code == 401
            assert _lifecycle_state(client, season_id) == "setup"
    finally:
        db_path.unlink(missing_ok=True)


def test_scorer_grant_scoped_to_a_different_season_is_denied(activation_client):
    client = activation_client
    other_season_id = _seed(client, 7014)["season"].season_id
    season_id = _seed(client, 7015)["season"].season_id
    _role_session(client, Role.SCORER, email="scoped-scorer-7015@example.com", season_id=other_season_id)

    assert client.get(f"/api/scorer/season-activation/{season_id}").status_code == 403
    response = client.post(f"/api/scorer/season-activation/{season_id}/activate", json={"reason": "wrong season"})
    assert response.status_code == 403, response.text
    assert _lifecycle_state(client, season_id) == "setup"

    # The same grant does cover the season it was actually issued for.
    assert client.get(f"/api/scorer/season-activation/{other_season_id}").status_code == 200


def test_session_write_without_csrf_token_is_refused(activation_client):
    client = activation_client
    season_id = _seed(client, 7016)["season"].season_id
    _role_session(client, Role.SCORER, email="csrf-scorer-7016@example.com", season_id=season_id)

    response = client.post(f"/api/scorer/season-activation/{season_id}/activate", json={"reason": "no csrf"})

    assert response.status_code == 403, response.text
    assert _lifecycle_state(client, season_id) == "setup"
