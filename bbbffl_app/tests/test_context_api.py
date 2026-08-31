"""End-to-end HTTP coverage for the shared multi-role/acting-context API
(roadmap package #107, issue #107): `app.routes.context`'s self-service
context-switch endpoints and Administrator-only role-grant management,
plus the Season Centre (#100/PR #106) retrofit that consumes them. Mirrors
`tests/test_auth_api.py`'s and `tests/test_season_centre_api.py`'s
isolated-database fixture patterns.
"""

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    db_path.unlink(missing_ok=True)


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf_token hidden field not found in rendered page"
    return match.group(1)


def _register_coach(app, *, email, name, password=PASSWORD):
    coach = app.state.identities.create_coach(name, email=email)
    from app.audit import ActorContext

    app.state.credentials.set_password(coach.coach_id, password, actor=ActorContext.anonymous_operator("admin"))
    return coach


def _login(client, *, email, password=PASSWORD):
    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf_token},
        cookies=login_page.cookies,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    session_cookie = response.cookies.get("bbbffl_session")
    assert session_cookie
    return session_cookie


def _csrf_for_session(client, session_cookie):
    """A fresh CSRF cookie/token pair issued for an authenticated session --
    any form-rendering GET issues one (app/csrf.py's double-submit design
    does not bind a token to the page that issued it), so `/account` works
    equally well as the source for a context-switch POST's token."""
    account = client.get("/account", cookies={"bbbffl_session": session_cookie})
    assert account.status_code == 200
    return _extract_csrf(account.text), account.cookies.get("bbbffl_csrf")


def _grant_role(client, *, coach_id, role, season_id=None):
    response = client.post("/api/admin/role-grants", json={"coach_id": coach_id, "role": role, "season_id": season_id})
    assert response.status_code == 200, response.text
    return response.json()


def _create_season(client, *, year, label):
    response = client.post("/api/admin/season-centre/seasons", json={"year": year, "label": label})
    assert response.status_code == 200, response.text
    return response.json()["season_id"]


def _create_entry(client, *, season_id, coach_id, team_name):
    response = client.post(
        f"/api/admin/season-centre/{season_id}/entries", json={"coach_id": coach_id, "team_name": team_name}
    )
    assert response.status_code == 200, response.text
    entries = {e["team_name"]: e for e in response.json()["entries"]}
    return entries[team_name]["season_entry_id"]


# -- GET /api/context ---------------------------------------------------


def test_context_defaults_to_coach_for_an_ordinary_coach_session(client):
    from app.main import app

    coach = _register_coach(app, email="coach@example.com", name="Ordinary Coach")
    session_cookie = _login(client, email="coach@example.com")

    response = client.get("/api/context", cookies={"bbbffl_session": session_cookie})
    assert response.status_code == 200
    body = response.json()
    assert body["coach_id"] == coach.coach_id
    assert body["active_role"] == "coach"
    assert (
        body["granted_roles"] == []
    )  # no season entry, no grants -- "coach" is unavailable and never active-by-default alone
    assert body["represented_season_entry"] is None
    assert body["is_replay_context"] is False


def test_context_for_the_legacy_admin_token_path_has_no_coach_identity(client):
    response = client.get("/api/context")
    body = response.json()
    assert body["coach_id"] is None
    assert body["active_role"] == "admin"
    assert body["granted_roles"] == ["admin"]


# -- Role grant administration -------------------------------------------


def test_only_an_administrator_can_grant_or_revoke_roles(client):
    from app.main import app

    operator = _register_coach(app, email="operator@example.com", name="Operator")
    session_cookie = _login(client, email="operator@example.com")

    # This coach has no admin authority of their own -- a coach-session
    # request (active role "coach") must be rejected, not silently ignored.
    grant = client.post(
        "/api/admin/role-grants",
        json={"coach_id": operator.coach_id, "role": "secretary"},
        cookies={"bbbffl_session": session_cookie},
    )
    assert grant.status_code == 403


def test_granting_an_ungrantable_role_is_rejected(client):
    from app.main import app

    operator = _register_coach(app, email="operator@example.com", name="Operator")
    response = client.post("/api/admin/role-grants", json={"coach_id": operator.coach_id, "role": "coach"})
    assert response.status_code == 400


def test_granting_a_season_scoped_administrator_role_is_rejected(client):
    from app.main import app

    operator = _register_coach(app, email="operator2@example.com", name="Operator Two")
    season_id = _create_season(client, year=2026, label="2026 Replay")
    response = client.post(
        "/api/admin/role-grants", json={"coach_id": operator.coach_id, "role": "admin", "season_id": season_id}
    )
    assert response.status_code == 400
    # Unscoped remains fine.
    assert (
        client.post("/api/admin/role-grants", json={"coach_id": operator.coach_id, "role": "admin"}).status_code == 200
    )


def test_grant_then_revoke_round_trip(client):
    from app.main import app

    operator = _register_coach(app, email="operator@example.com", name="Operator")
    grant = _grant_role(client, coach_id=operator.coach_id, role="scorer")

    active = client.get(f"/api/admin/role-grants?coach_id={operator.coach_id}").json()
    assert [g["role"] for g in active if not g["revoked_at"]] == ["scorer"]

    revoke = client.post(f"/api/admin/role-grants/{grant['grant_id']}/revoke")
    assert revoke.status_code == 200
    after = client.get(f"/api/admin/role-grants?coach_id={operator.coach_id}").json()
    assert after[0]["revoked_at"] is not None

    # Idempotent/not-found: revoking again (or an unknown grant) is a clean 404.
    assert client.post(f"/api/admin/role-grants/{grant['grant_id']}/revoke").status_code == 404


# -- Role switching -------------------------------------------------------


def test_a_multi_role_coach_can_switch_active_role_without_reauthenticating(client):
    from app.main import app

    coach = _register_coach(app, email="multi@example.com", name="Multi Role")
    _grant_role(client, coach_id=coach.coach_id, role="scorer")
    _grant_role(client, coach_id=coach.coach_id, role="secretary")
    session_cookie = _login(client, email="multi@example.com")
    csrf_token, csrf_cookie = _csrf_for_session(client, session_cookie)

    ctx = client.get("/api/context", cookies={"bbbffl_session": session_cookie}).json()
    assert sorted(ctx["granted_roles"]) == ["scorer", "secretary"]
    assert ctx["active_role"] == "coach"

    switched = client.post(
        "/api/context/role",
        json={"role": "secretary"},
        cookies={"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["active_role"] == "secretary"
    # No re-authentication happened -- the same session/authenticated actor.
    assert switched.json()["coach_id"] == coach.coach_id

    still = client.get("/api/context", cookies={"bbbffl_session": session_cookie}).json()
    assert still["active_role"] == "secretary"


def test_switching_to_an_ungranted_role_is_rejected(client):
    from app.main import app

    _register_coach(app, email="plain@example.com", name="Plain Coach")
    session_cookie = _login(client, email="plain@example.com")
    csrf_token, csrf_cookie = _csrf_for_session(client, session_cookie)

    response = client.post(
        "/api/context/role",
        json={"role": "admin"},
        cookies={"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 403


def test_switching_role_without_a_csrf_token_is_rejected(client):
    from app.main import app

    coach = _register_coach(app, email="csrf@example.com", name="CSRF Coach")
    _grant_role(client, coach_id=coach.coach_id, role="scorer")
    session_cookie = _login(client, email="csrf@example.com")

    response = client.post("/api/context/role", json={"role": "scorer"}, cookies={"bbbffl_session": session_cookie})
    assert response.status_code == 403


def test_the_legacy_admin_token_credential_cannot_switch_context(client):
    """The shared operator token has no per-person session row to switch --
    see docs/acting-context.md's scope boundary."""
    response = client.post("/api/context/role", json={"role": "scorer"})
    assert response.status_code == 401


# -- Represented-entry switching (delegated roles only) --------------------


def test_coach_mode_cannot_select_a_represented_entry(client):
    from app.main import app

    coach = _register_coach(app, email="coach2@example.com", name="Coach Two")
    season_id = _create_season(client, year=2027, label="2027 Season")
    entry_id = _create_entry(client, season_id=season_id, coach_id=coach.coach_id, team_name="Coach Two's Team")
    session_cookie = _login(client, email="coach2@example.com")
    csrf_token, csrf_cookie = _csrf_for_session(client, session_cookie)

    response = client.post(
        "/api/context/represented-entry",
        json={"season_entry_id": entry_id},
        cookies={"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 403


def test_a_replay_operator_grant_can_represent_replay_entries_but_not_live_season_entries(client):
    from app.main import app

    operator = _register_coach(app, email="replay-op@example.com", name="Replay Operator")
    replay_coach = _register_coach(app, email="replay-coach@example.com", name="Replay Coach")
    live_coach = _register_coach(app, email="live-coach@example.com", name="Live Coach")

    replay_season = _create_season(client, year=2026, label="2026 Replay")
    live_season = _create_season(client, year=2027, label="2027 Season")
    replay_entry = _create_entry(
        client, season_id=replay_season, coach_id=replay_coach.coach_id, team_name="Replay Team"
    )
    live_entry = _create_entry(client, season_id=live_season, coach_id=live_coach.coach_id, team_name="Live Team")

    _grant_role(client, coach_id=operator.coach_id, role="replay_operator", season_id=replay_season)
    session_cookie = _login(client, email="replay-op@example.com")
    csrf_token, csrf_cookie = _csrf_for_session(client, session_cookie)
    cookies = {"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie}
    headers = {"X-CSRF-Token": csrf_token}

    client.post("/api/context/role", json={"role": "replay_operator"}, cookies=cookies, headers=headers)

    # Rapid switching among replay entries succeeds and is unmistakably flagged.
    ok = client.post(
        "/api/context/represented-entry", json={"season_entry_id": replay_entry}, cookies=cookies, headers=headers
    )
    assert ok.status_code == 200
    assert ok.json()["represented_season_entry"]["season_entry_id"] == replay_entry
    assert ok.json()["is_replay_context"] is True

    # The live/current season is never reachable through this grant.
    blocked = client.post(
        "/api/context/represented-entry", json={"season_entry_id": live_entry}, cookies=cookies, headers=headers
    )
    assert blocked.status_code == 403

    entries = client.get(f"/api/context/entries?season_id={replay_season}", cookies=cookies).json()
    assert [e["season_entry_id"] for e in entries] == [replay_entry]
    assert client.get(f"/api/context/entries?season_id={live_season}", cookies=cookies).json() == []

    # The authenticated operator never changes.
    still_me = client.get("/api/context", cookies=cookies).json()
    assert still_me["coach_id"] == operator.coach_id


def test_switching_active_role_clears_the_previously_represented_entry(client):
    from app.main import app

    operator = _register_coach(app, email="dual@example.com", name="Dual Role")
    coach = _register_coach(app, email="entry-owner@example.com", name="Entry Owner")
    season_id = _create_season(client, year=2027, label="2027 Season")
    entry_id = _create_entry(client, season_id=season_id, coach_id=coach.coach_id, team_name="Entry Owner's Team")
    _grant_role(client, coach_id=operator.coach_id, role="scorer")
    _grant_role(client, coach_id=operator.coach_id, role="secretary")
    session_cookie = _login(client, email="dual@example.com")
    csrf_token, csrf_cookie = _csrf_for_session(client, session_cookie)
    cookies = {"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie}
    headers = {"X-CSRF-Token": csrf_token}

    client.post("/api/context/role", json={"role": "scorer"}, cookies=cookies, headers=headers)
    client.post("/api/context/represented-entry", json={"season_entry_id": entry_id}, cookies=cookies, headers=headers)
    assert client.get("/api/context", cookies=cookies).json()["represented_season_entry"]["season_entry_id"] == entry_id

    client.post("/api/context/role", json={"role": "secretary"}, cookies=cookies, headers=headers)
    assert client.get("/api/context", cookies=cookies).json()["represented_season_entry"] is None


# -- Season Centre retrofit: Secretary need not be Administrator -----------


def test_a_secretary_role_session_can_use_season_centre_without_the_admin_token(client):
    from app.main import app

    officer = _register_coach(app, email="officer@example.com", name="League Officer")
    _grant_role(client, coach_id=officer.coach_id, role="secretary")
    session_cookie = _login(client, email="officer@example.com")
    csrf_token, csrf_cookie = _csrf_for_session(client, session_cookie)
    cookies = {"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie}
    headers = {"X-CSRF-Token": csrf_token}

    client.post("/api/context/role", json={"role": "secretary"}, cookies=cookies, headers=headers)

    # No X-Admin-Token header anywhere below -- the session cookie alone
    # (carrying Secretary authority) must be sufficient.
    created = client.post(
        "/api/admin/season-centre/seasons", json={"year": 2028, "label": "2028 Season"}, cookies=cookies
    )
    assert created.status_code == 200, created.text


def test_a_season_scoped_secretary_cannot_manage_a_different_seasons_entries(client):
    """Regression for a code-review finding: a Secretary grant scoped to
    one season (e.g. confined to the 2026 replay) must never be usable to
    rename a team or reassign a coach in a *different* season through
    Season Centre, even though `require_secretary_or_admin` alone only
    checks the role name, not which season(s) it was granted for."""
    from app.main import app

    officer = _register_coach(app, email="scoped-officer@example.com", name="Scoped Officer")
    coach = _register_coach(app, email="live-season-coach@example.com", name="Live Season Coach")
    allowed_season = _create_season(client, year=2026, label="2026 Replay")
    other_season = _create_season(client, year=2027, label="2027 Season")
    other_entry = _create_entry(client, season_id=other_season, coach_id=coach.coach_id, team_name="Live Team")
    _grant_role(client, coach_id=officer.coach_id, role="secretary", season_id=allowed_season)

    session_cookie = _login(client, email="scoped-officer@example.com")
    csrf_token, csrf_cookie = _csrf_for_session(client, session_cookie)
    cookies = {"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie}
    headers = {"X-CSRF-Token": csrf_token}
    client.post("/api/context/role", json={"role": "secretary"}, cookies=cookies, headers=headers)

    # The allowed (granted) season works normally.
    allowed = client.get(f"/api/admin/season-centre/{allowed_season}", cookies=cookies)
    assert allowed.status_code == 200

    # A different season -- view, create-entry, rename, and reassign are
    # all refused, not just the ones that happen to be exercised elsewhere.
    assert client.get(f"/api/admin/season-centre/{other_season}", cookies=cookies).status_code == 403
    assert (
        client.post(
            f"/api/admin/season-centre/{other_season}/entries",
            json={"coach_id": coach.coach_id, "team_name": "Another Team"},
            cookies=cookies,
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/admin/season-centre/entries/{other_entry}/team-name",
            json={"team_name": "Renamed"},
            cookies=cookies,
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/admin/season-centre/entries/{other_entry}/coach",
            json={"coach_id": officer.coach_id},
            cookies=cookies,
        ).status_code
        == 403
    )


def test_a_scorer_only_session_cannot_use_season_centre(client):
    from app.main import app

    scorer = _register_coach(app, email="scorer-only@example.com", name="Scorer Only")
    _grant_role(client, coach_id=scorer.coach_id, role="scorer")
    session_cookie = _login(client, email="scorer-only@example.com")
    csrf_token, csrf_cookie = _csrf_for_session(client, session_cookie)
    cookies = {"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie}
    headers = {"X-CSRF-Token": csrf_token}

    client.post("/api/context/role", json={"role": "scorer"}, cookies=cookies, headers=headers)

    response = client.post(
        "/api/admin/season-centre/seasons", json={"year": 2028, "label": "2028 Season"}, cookies=cookies
    )
    assert response.status_code == 403


def test_a_revoked_grant_no_longer_confers_authority_on_the_next_request(client):
    from app.main import app

    officer = _register_coach(app, email="revoked@example.com", name="Revoked Officer")
    grant = _grant_role(client, coach_id=officer.coach_id, role="secretary")
    session_cookie = _login(client, email="revoked@example.com")
    csrf_token, csrf_cookie = _csrf_for_session(client, session_cookie)
    cookies = {"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie}
    headers = {"X-CSRF-Token": csrf_token}

    client.post("/api/context/role", json={"role": "secretary"}, cookies=cookies, headers=headers)
    assert client.get("/api/context", cookies=cookies).json()["active_role"] == "secretary"

    # The TestClient persists cookies across requests; clear the jar so this
    # admin-only revoke call uses the tokenless-dev ambient admin authority
    # rather than accidentally riding the officer's own carried-over session
    # cookie (which would correctly be refused -- Secretary may not revoke
    # roles -- and leave the grant untouched, defeating this test's point).
    client.cookies.clear()
    revoke = client.post(f"/api/admin/role-grants/{grant['grant_id']}/revoke")
    assert revoke.status_code == 200

    # Self-healing: the very next request silently falls back to "coach"
    # rather than continuing to act as Secretary.
    after = client.get("/api/context", cookies=cookies).json()
    assert after["active_role"] == "coach"
    blocked = client.post(
        "/api/admin/season-centre/seasons", json={"year": 2029, "label": "2029 Season"}, cookies=cookies
    )
    assert blocked.status_code == 403
