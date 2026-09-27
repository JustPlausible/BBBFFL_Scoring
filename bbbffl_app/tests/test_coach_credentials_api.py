"""HTTP-level coverage for the Administrator browser coach-credential
provisioning/reset workflow (issue #238): the unauthenticated page shell
(`GET /admin/coach-credentials`) and the JSON API it drives
(`GET`/`POST /api/admin/coach-credentials`, `app/routes/coach_credentials.py`).

Mirrors `tests/test_admin_dashboard_api.py`'s established conventions for
this codebase's session-native Administrator dashboards: a SQLite
`dashboard_client` fixture with the legacy `BBBFFL_ADMIN_TOKEN` unset (an
"open operator" dev/test posture, matching `app.authorization.
resolve_principal`'s own documented fallback), and `client.app.
dependency_overrides` to exercise each role boundary directly. Authorization
denial tests override `resolve_principal` (the *inner* dependency), not
`require_admin_credentials` itself -- overriding the latter would replace
the very check under test, the same distinction
`tests/test_admin_dashboard_api.py`'s own denial tests rely on.

The page shell is deliberately unauthenticated (Codex review, PR #252, P1:
a plain page load cannot attach the legacy `X-Admin-Token` header, so
gating the shell itself would make this page unreachable for the
production-bootstrap operator whose only authority is that token, before
any coach has ever been granted Administrator) -- so authorization is
proven against the JSON API, and the page GET is only used here to obtain
the CSRF cookie/token pair every mutating JSON POST still requires.

`tests/test_auth_api.py::test_admin_can_reset_a_coachs_password_by_coach_id`
and its siblings already cover the pre-existing JSON API
(`POST /api/admin/coach-credential`, singular) and are re-run unmodified
alongside this file to prove that surface, and ordinary coach login/session
behaviour, are unaffected by this browser addition.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.authorization import Principal, Role

ADMIN = ActorContext.anonymous_operator("admin")


@pytest.fixture
def dashboard_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _override(client, dependency, principal):
    client.app.dependency_overrides[dependency] = lambda: principal


def _clear_overrides(client):
    client.app.dependency_overrides.clear()


def _admin_principal(coach_id="admin-1", display_name="Standing Admin"):
    return Principal(Role.ADMIN, coach_id, display_name, granted_roles=frozenset({Role.ADMIN}), session_id="s1")


def _register_coach(client, email="coach@example.com", name="Test Coach", password=None):
    coach = client.app.state.identities.create_coach(name, email=email)
    if password is not None:
        client.app.state.credentials.set_password(coach.coach_id, password, actor=ADMIN)
    return coach


def _csrf_token(client):
    """The page shell is unauthenticated (see module docstring), so any GET
    to it -- regardless of dependency overrides -- issues a fresh CSRF
    cookie/token pair. Both halves of the double-submit pair share the same
    value, so the cookie value doubles as the header value the JSON POST
    below submits."""
    response = client.get("/admin/coach-credentials")
    assert response.status_code == 200
    token = response.cookies.get("bbbffl_csrf")
    assert token
    return token


def _submit(client, *, coach_id, new_password, confirm_password=None, reason=None, csrf_token=None, principal=None):
    token = _csrf_token(client)
    if principal is not None:
        from app.routes.coach_credentials import require_admin_credentials

        _override(client, require_admin_credentials, principal)
    return client.post(
        "/api/admin/coach-credentials",
        json={
            "coach_id": coach_id,
            "new_password": new_password,
            "confirm_password": confirm_password if confirm_password is not None else new_password,
            "reason": reason,
        },
        headers={"X-CSRF-Token": csrf_token if csrf_token is not None else token},
    )


# -- Page shell -------------------------------------------------------------


def test_page_shell_loads_without_authentication(dashboard_client, monkeypatch):
    """Codex review, PR #252 (P1): the page shell itself must not require
    the legacy X-Admin-Token header (a plain page load cannot attach a
    custom header at all), so it stays reachable for the production-
    bootstrap operator whose only authority is that token."""
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "a-real-configured-token")
    response = dashboard_client.get("/admin/coach-credentials")
    assert response.status_code == 200
    assert response.cookies.get("bbbffl_csrf")


def test_page_shell_never_embeds_coach_data(dashboard_client):
    """The shell renders no coach-specific markup server-side; all coach
    data is fetched client-side from the authorization-gated JSON API."""
    _register_coach(dashboard_client, email="steve@example.com", name="Steve Hardingham")
    response = dashboard_client.get("/admin/coach-credentials")
    assert response.status_code == 200
    assert "Steve Hardingham" not in response.text
    assert "steve@example.com" not in response.text


# -- Authorization (JSON API) -------------------------------------------------


def test_administrator_can_view_the_roster(dashboard_client):
    from app.routes.coach_credentials import require_admin_credentials

    client = dashboard_client
    coach = _register_coach(client, email="steve@example.com", name="Steve Hardingham")
    _override(client, require_admin_credentials, _admin_principal())
    try:
        response = client.get("/api/admin/coach-credentials")
    finally:
        _clear_overrides(client)
    assert response.status_code == 200
    roster = response.json()["roster"]
    row = next(r for r in roster if r["coach_id"] == coach.coach_id)
    assert row["display_name"] == "Steve Hardingham"
    assert row["has_credential"] is False


def test_unauthenticated_api_request_is_rejected(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "a-real-configured-token")
    monkeypatch.setenv("BBBFFL_SESSION_SECRET", "a-real-configured-session-secret")

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/api/admin/coach-credentials")
        assert response.status_code == 401
    db_path.unlink(missing_ok=True)


def test_legacy_admin_token_can_access_the_api(monkeypatch):
    """The whole point of keeping the JSON API's authorization identical to
    the page shell's own bootstrap path (Codex review, PR #252, P1): a
    request carrying only the legacy shared token -- no coach session at
    all -- must still work."""
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "a-real-configured-token")
    monkeypatch.setenv("BBBFFL_SESSION_SECRET", "a-real-configured-session-secret")

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/api/admin/coach-credentials", headers={"X-Admin-Token": "a-real-configured-token"})
        assert response.status_code == 200
    db_path.unlink(missing_ok=True)


@pytest.mark.parametrize("role", [Role.SCORER, Role.COACH, Role.SECRETARY, Role.SPECTATOR])
def test_non_admin_roles_cannot_access_the_credential_api(dashboard_client, role):
    """The current capability model (`app.authorization.CAPABILITIES`) grants
    no credential-management authority to Scorer or any other non-
    Administrator role -- the same authority the existing JSON endpoint
    already requires (`app.routes.admin.require_admin`). This is a
    deliberate scope decision (see the route module's docstring), not an
    oversight: widening it would be an authorization design change outside
    issue #238's remit."""
    from app.authorization import resolve_principal

    client = dashboard_client
    coach = _register_coach(client)
    principal = Principal(role, "operator-1", "Operator", granted_roles=frozenset({role}), session_id="s1")
    # Role.SPECTATOR is `require_authenticated`'s own 401 ("not signed in at
    # all"), distinct from a signed-in-but-unauthorised role's 403 -- both
    # are rejections, neither reaches the roster or the write.
    expected_status = 401 if role is Role.SPECTATOR else 403
    _override(client, resolve_principal, principal)
    try:
        get_response = client.get("/api/admin/coach-credentials")
        assert get_response.status_code == expected_status

        token = _csrf_token(client)
        post_response = client.post(
            "/api/admin/coach-credentials",
            json={
                "coach_id": coach.coach_id,
                "new_password": "brand-new-password-456",
                "confirm_password": "brand-new-password-456",
                "reason": None,
            },
            headers={"X-CSRF-Token": token},
        )
        assert post_response.status_code == expected_status
    finally:
        _clear_overrides(client)


# -- Provisioning / reset -------------------------------------------------


def test_administrator_can_provision_a_new_coachs_password(dashboard_client):
    client = dashboard_client
    coach = _register_coach(client, email="coach@example.com")

    response = _submit(
        client, coach_id=coach.coach_id, new_password="brand-new-password-456", principal=_admin_principal()
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"coach_id": coach.coach_id, "status": "provisioned"}
    _clear_overrides(client)

    csrf_token, cookies = _login_form(client)
    login = client.post(
        "/login",
        data={"email": "coach@example.com", "password": "brand-new-password-456", "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.cookies.get("bbbffl_session")


def test_administrator_can_reset_an_existing_coachs_password(dashboard_client):
    client = dashboard_client
    coach = _register_coach(client, email="coach@example.com", password="original-password-123")

    csrf_token, cookies = _login_form(client)
    login = client.post(
        "/login",
        data={"email": "coach@example.com", "password": "original-password-123", "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    session_cookie = login.cookies.get("bbbffl_session")
    assert client.get("/account", cookies={"bbbffl_session": session_cookie}).status_code == 200

    response = _submit(
        client, coach_id=coach.coach_id, new_password="brand-new-password-456", principal=_admin_principal()
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"coach_id": coach.coach_id, "status": "reset"}
    _clear_overrides(client)

    # The coach's previous session must no longer authenticate.
    after_reset = client.get("/account", cookies={"bbbffl_session": session_cookie}, follow_redirects=False)
    assert after_reset.status_code == 303


def test_administrator_can_reset_their_own_password_without_a_broken_response(dashboard_client):
    """Codex review, PR #252 (P2): `reset_password` revokes every valid
    session for the affected coach, including -- if an Administrator
    resets their own credential -- the very session authenticating this
    request. The JSON API must still return a normal, successful response
    for the request already in flight (FastAPI never re-checks
    authorization to send a response it already computed); only a
    *subsequent* request would see the revoked session."""
    client = dashboard_client
    coach = _register_coach(client, email="admin-coach@example.com", password="original-password-123")
    client.app.state.role_grants.grant(coach.coach_id, "admin", actor=ADMIN)

    csrf_token, cookies = _login_form(client)
    login = client.post(
        "/login",
        data={"email": "admin-coach@example.com", "password": "original-password-123", "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    session_token = login.cookies.get("bbbffl_session")
    session = client.app.state.sessions.get_valid(session_token)
    client.app.state.acting_context.activate_role(
        coach_id=coach.coach_id, session_id=session.session_id, role="admin", actor=ActorContext.coach(coach.coach_id)
    )

    page = client.get("/admin/coach-credentials", cookies={"bbbffl_session": session_token})
    page_csrf = page.cookies.get("bbbffl_csrf")
    response = client.post(
        "/api/admin/coach-credentials",
        json={
            "coach_id": coach.coach_id,
            "new_password": "brand-new-password-456",
            "confirm_password": "brand-new-password-456",
            "reason": None,
        },
        headers={"X-CSRF-Token": page_csrf},
        cookies={"bbbffl_session": session_token, "bbbffl_csrf": page_csrf},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"coach_id": coach.coach_id, "status": "reset"}

    # The session that authenticated this very request is now revoked.
    assert client.app.state.sessions.get_valid(session_token) is None


def test_password_confirmation_mismatch_is_rejected(dashboard_client):
    client = dashboard_client
    coach = _register_coach(client, password="original-password-123")

    response = _submit(
        client,
        coach_id=coach.coach_id,
        new_password="brand-new-password-456",
        confirm_password="does-not-match",
        principal=_admin_principal(),
    )
    assert response.status_code == 400
    assert "do not match" in response.json()["detail"]
    _clear_overrides(client)

    csrf_token, cookies = _login_form(client)
    still_works = client.post(
        "/login",
        data={"email": "coach@example.com", "password": "original-password-123", "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    assert still_works.status_code == 303


def test_weak_password_is_rejected(dashboard_client):
    client = dashboard_client
    coach = _register_coach(client, password="original-password-123")

    response = _submit(client, coach_id=coach.coach_id, new_password="short", principal=_admin_principal())
    assert response.status_code == 400
    assert "at least" in response.json()["detail"]
    _clear_overrides(client)

    csrf_token, cookies = _login_form(client)
    still_works = client.post(
        "/login",
        data={"email": "coach@example.com", "password": "original-password-123", "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    assert still_works.status_code == 303


def test_unknown_coach_id_is_rejected_not_a_500(dashboard_client):
    """Regression, matching the JSON API's own
    `test_admin_credential_reset_for_unknown_coach_id_returns_404_not_500`:
    a tampered request naming a nonexistent coach_id must be rejected
    cleanly, never surface as an uncaught foreign-key error."""
    client = dashboard_client
    _register_coach(client)

    response = _submit(
        client, coach_id="does-not-exist", new_password="brand-new-password-456", principal=_admin_principal()
    )
    assert response.status_code == 404


def test_empty_coach_selection_is_rejected(dashboard_client):
    client = dashboard_client
    _register_coach(client)

    response = _submit(client, coach_id="", new_password="brand-new-password-456", principal=_admin_principal())
    assert response.status_code == 404


# -- CSRF -----------------------------------------------------------------


def test_csrf_is_enforced(dashboard_client):
    client = dashboard_client
    coach = _register_coach(client, password="original-password-123")

    response = _submit(
        client,
        coach_id=coach.coach_id,
        new_password="brand-new-password-456",
        csrf_token="forged-token",
        principal=_admin_principal(),
    )
    assert response.status_code == 403
    _clear_overrides(client)

    csrf_token, cookies = _login_form(client)
    still_works = client.post(
        "/login",
        data={"email": "coach@example.com", "password": "original-password-123", "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    assert still_works.status_code == 303


def test_missing_csrf_header_is_enforced(dashboard_client):
    from app.routes.coach_credentials import require_admin_credentials

    client = dashboard_client
    coach = _register_coach(client)
    _csrf_token(client)
    _override(client, require_admin_credentials, _admin_principal())
    try:
        response = client.post(
            "/api/admin/coach-credentials",
            json={
                "coach_id": coach.coach_id,
                "new_password": "brand-new-password-456",
                "confirm_password": "brand-new-password-456",
                "reason": None,
            },
        )
    finally:
        _clear_overrides(client)
    assert response.status_code == 403


# -- Audit / privacy -------------------------------------------------------


def test_audit_records_the_authenticated_operator_not_the_coach(dashboard_client):
    client = dashboard_client
    coach = _register_coach(client)
    operator = _admin_principal(coach_id="admin-42", display_name="Real Admin")

    response = _submit(client, coach_id=coach.coach_id, new_password="brand-new-password-456", principal=operator)
    assert response.status_code == 200
    _clear_overrides(client)

    events = client.app.state.audit_events.list_events(entity_type="auth.credential", entity_id=coach.coach_id)
    assert events
    event = events[-1]
    assert event.actor_type == "anonymous_operator"
    assert event.actor_id == "admin-42"
    assert event.actor_id != coach.coach_id
    payload = str(event.before_state) + str(event.after_state) + str(event.payload)
    assert "brand-new-password-456" not in payload


def test_reason_is_recorded_when_provided(dashboard_client):
    client = dashboard_client
    coach = _register_coach(client)

    response = _submit(
        client,
        coach_id=coach.coach_id,
        new_password="brand-new-password-456",
        reason="season onboarding",
        principal=_admin_principal(),
    )
    assert response.status_code == 200
    _clear_overrides(client)

    events = client.app.state.audit_events.list_events(entity_type="auth.credential", entity_id=coach.coach_id)
    assert events[-1].reason == "season onboarding"


def test_roster_never_exposes_password_hashes(dashboard_client):
    from app.routes.coach_credentials import require_admin_credentials

    client = dashboard_client
    coach = _register_coach(client, password="original-password-123")
    row = client.app.state.database.execute(
        "SELECT password_hash FROM coach_credential WHERE coach_id = ?", (coach.coach_id,)
    ).fetchone()
    stored_hash = row["password_hash"]

    _override(client, require_admin_credentials, _admin_principal())
    try:
        response = client.get("/api/admin/coach-credentials")
    finally:
        _clear_overrides(client)
    assert response.status_code == 200
    body_text = response.text
    assert stored_hash not in body_text
    assert "password_hash" not in body_text
    assert "original-password-123" not in body_text


def test_human_readable_team_selection_no_uuid_entry_required(dashboard_client):
    """Issue #238's core requirement: an operator identifies a coach by
    name/team, never by typing or copying a `coach_id`. The roster response
    carries `coach_id` only as a machine-readable field the page's own JS
    uses as a `<select>` option's value -- an operator interacting with the
    rendered page never types or copies it."""
    from app.routes.coach_credentials import require_admin_credentials

    client = dashboard_client
    season = client.app.state.seasons.create_season(2027, "2027 Season")
    coach = client.app.state.identities.create_coach("Jordan Example", email="jordan@example.com")
    client.app.state.identities.create_entry(season.season_id, "licence-jordan", coach.coach_id, "Jordan's Juggernauts")

    _override(client, require_admin_credentials, _admin_principal())
    try:
        response = client.get("/api/admin/coach-credentials")
    finally:
        _clear_overrides(client)
    assert response.status_code == 200
    row = next(r for r in response.json()["roster"] if r["coach_id"] == coach.coach_id)
    assert row["display_name"] == "Jordan Example"
    assert row["teams"] == ["Jordan's Juggernauts (2027 Season)"]


def _login_form(client):
    """Fetch the coach sign-in form's CSRF token/cookie, matching
    `tests/test_auth_api.py`'s own `_get_login_form` helper (not imported
    directly to avoid coupling this file to that one's fixtures)."""
    import re

    response = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1), {"bbbffl_csrf": response.cookies.get("bbbffl_csrf")}
