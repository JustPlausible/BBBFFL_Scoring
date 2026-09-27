"""HTTP-level coverage for the Administrator browser coach-credential
provisioning/reset workflow (issue #238): `GET`/`POST /admin/coach-
credentials` (`app/routes/coach_credentials.py`).

Mirrors `tests/test_admin_dashboard_api.py`'s established conventions for
this codebase's session-native Administrator dashboards: a SQLite
`dashboard_client` fixture with the legacy `BBBFFL_ADMIN_TOKEN` unset (an
"open operator" dev/test posture, matching `app.authorization.
resolve_principal`'s own documented fallback), and `client.app.
dependency_overrides` to exercise each role boundary directly rather than
building a full coach-session login for every scenario. The end-to-end
CSRF/session-cookie flow is only needed for the mutating POST tests, so
those fetch the page for real (to obtain the double-submit CSRF cookie/
token pair) rather than overriding CSRF away.

`tests/test_auth_api.py::test_admin_can_reset_a_coachs_password_by_coach_id`
and its siblings already cover the underlying JSON API
(`POST /api/admin/coach-credential`) and are re-run unmodified alongside
this file to prove that surface, and ordinary coach login/session
behaviour, are unaffected by this browser addition.
"""

import re
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


def _get_form(client):
    from app.routes.coach_credentials import require_admin_credentials

    _override(client, require_admin_credentials, _admin_principal())
    response = client.get("/admin/coach-credentials")
    assert response.status_code == 200, response.text
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, "csrf_token hidden field not found in rendered page"
    return response.text, match.group(1)


def _submit(client, *, coach_id, new_password, confirm_password=None, reason="", csrf_token=None, principal=None):
    from app.routes.coach_credentials import require_admin_credentials

    page, token = _get_form(client)
    if principal is not None:
        _override(client, require_admin_credentials, principal)
    data = {
        "coach_id": coach_id,
        "new_password": new_password,
        "confirm_password": confirm_password if confirm_password is not None else new_password,
        "reason": reason,
        "csrf_token": csrf_token if csrf_token is not None else token,
    }
    return client.post("/admin/coach-credentials", data=data, follow_redirects=False), page


# -- Authorization ------------------------------------------------------


def test_administrator_can_view_the_page(dashboard_client):
    from app.routes.coach_credentials import require_admin_credentials

    client = dashboard_client
    coach = _register_coach(client, email="steve@example.com", name="Steve Hardingham")
    _override(client, require_admin_credentials, _admin_principal())
    try:
        response = client.get("/admin/coach-credentials")
    finally:
        _clear_overrides(client)
    assert response.status_code == 200
    assert "Steve Hardingham" in response.text
    assert coach.coach_id in response.text  # only ever inside an <option value="...">, never typed by an operator
    assert "no password yet" in response.text


def test_unauthenticated_request_is_rejected(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "a-real-configured-token")
    monkeypatch.setenv("BBBFFL_SESSION_SECRET", "a-real-configured-session-secret")

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/admin/coach-credentials")
        assert response.status_code == 401
    db_path.unlink(missing_ok=True)


@pytest.mark.parametrize("role", [Role.SCORER, Role.COACH, Role.SECRETARY, Role.SPECTATOR])
def test_non_admin_roles_cannot_view_or_submit_the_page(dashboard_client, role):
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
    # are rejections, neither reaches the page.
    expected_status = 401 if role is Role.SPECTATOR else 403
    _override(client, resolve_principal, principal)
    try:
        get_response = client.get("/admin/coach-credentials")
        assert get_response.status_code == expected_status

        post_response = client.post(
            "/admin/coach-credentials",
            data={
                "coach_id": coach.coach_id,
                "new_password": "brand-new-password-456",
                "confirm_password": "brand-new-password-456",
                "reason": "",
                "csrf_token": "irrelevant",
            },
        )
        assert post_response.status_code == expected_status
    finally:
        _clear_overrides(client)


# -- Provisioning / reset -------------------------------------------------


def test_administrator_can_provision_a_new_coachs_password(dashboard_client):
    client = dashboard_client
    coach = _register_coach(client, email="coach@example.com")

    response, _ = _submit(
        client,
        coach_id=coach.coach_id,
        new_password="brand-new-password-456",
        principal=_admin_principal(),
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/coach-credentials?notice=provisioned"
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

    response, _ = _submit(
        client,
        coach_id=coach.coach_id,
        new_password="brand-new-password-456",
        principal=_admin_principal(),
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/coach-credentials?notice=reset"
    _clear_overrides(client)

    # The coach's previous session must no longer authenticate.
    after_reset = client.get("/account", cookies={"bbbffl_session": session_cookie}, follow_redirects=False)
    assert after_reset.status_code == 303


def test_password_confirmation_mismatch_is_rejected(dashboard_client):
    client = dashboard_client
    coach = _register_coach(client, password="original-password-123")

    response, _ = _submit(
        client,
        coach_id=coach.coach_id,
        new_password="brand-new-password-456",
        confirm_password="does-not-match",
        principal=_admin_principal(),
    )
    assert response.status_code == 400
    assert "do not match" in response.text
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

    response, _ = _submit(
        client,
        coach_id=coach.coach_id,
        new_password="short",
        principal=_admin_principal(),
    )
    assert response.status_code == 400
    assert "at least" in response.text
    _clear_overrides(client)

    csrf_token, cookies = _login_form(client)
    still_works = client.post(
        "/login",
        data={"email": "coach@example.com", "password": "original-password-123", "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    assert still_works.status_code == 303


def test_missing_coach_selection_is_rejected(dashboard_client):
    client = dashboard_client
    _register_coach(client)

    response, _ = _submit(
        client,
        coach_id="",
        new_password="brand-new-password-456",
        principal=_admin_principal(),
    )
    assert response.status_code == 400
    assert "Choose a coach" in response.text


def test_unknown_coach_id_is_rejected_not_a_500(dashboard_client):
    """Regression, matching the JSON API's own
    `test_admin_credential_reset_for_unknown_coach_id_returns_404_not_500`:
    a form tampered to submit a nonexistent coach_id must be rejected
    cleanly, never surface as an uncaught foreign-key error."""
    client = dashboard_client
    _register_coach(client)

    response, _ = _submit(
        client,
        coach_id="does-not-exist",
        new_password="brand-new-password-456",
        principal=_admin_principal(),
    )
    assert response.status_code == 400
    assert "Choose a coach" in response.text


# -- CSRF -----------------------------------------------------------------


def test_csrf_is_enforced(dashboard_client):
    client = dashboard_client
    coach = _register_coach(client, password="original-password-123")

    response, _ = _submit(
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


# -- Audit / privacy -------------------------------------------------------


def test_audit_records_the_authenticated_operator_not_the_coach(dashboard_client):
    client = dashboard_client
    coach = _register_coach(client)
    operator = _admin_principal(coach_id="admin-42", display_name="Real Admin")

    response, _ = _submit(
        client,
        coach_id=coach.coach_id,
        new_password="brand-new-password-456",
        principal=operator,
    )
    assert response.status_code == 303
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

    response, _ = _submit(
        client,
        coach_id=coach.coach_id,
        new_password="brand-new-password-456",
        reason="season onboarding",
        principal=_admin_principal(),
    )
    assert response.status_code == 303
    _clear_overrides(client)

    events = client.app.state.audit_events.list_events(entity_type="auth.credential", entity_id=coach.coach_id)
    assert events[-1].reason == "season onboarding"


def test_page_never_exposes_password_hashes(dashboard_client):
    from app.routes.coach_credentials import require_admin_credentials

    client = dashboard_client
    coach = _register_coach(client, password="original-password-123")
    row = client.app.state.database.execute(
        "SELECT password_hash FROM coach_credential WHERE coach_id = ?", (coach.coach_id,)
    ).fetchone()
    stored_hash = row["password_hash"]

    _override(client, require_admin_credentials, _admin_principal())
    try:
        response = client.get("/admin/coach-credentials")
    finally:
        _clear_overrides(client)
    assert response.status_code == 200
    assert stored_hash not in response.text
    assert "password_hash" not in response.text
    assert "original-password-123" not in response.text


def test_human_readable_team_selection_no_uuid_entry_required(dashboard_client):
    """Issue #238's core requirement: an operator identifies a coach by
    name/team, never by typing or copying a `coach_id`."""
    from app.routes.coach_credentials import require_admin_credentials

    client = dashboard_client
    season = client.app.state.seasons.create_season(2027, "2027 Season")
    coach = client.app.state.identities.create_coach("Jordan Example", email="jordan@example.com")
    client.app.state.identities.create_entry(season.season_id, "licence-jordan", coach.coach_id, "Jordan's Juggernauts")

    _override(client, require_admin_credentials, _admin_principal())
    try:
        response = client.get("/admin/coach-credentials")
    finally:
        _clear_overrides(client)
    assert response.status_code == 200
    assert "Jordan Example" in response.text
    assert "Jordan&#39;s Juggernauts" in response.text  # Jinja auto-escapes the apostrophe
    assert "2027 Season" in response.text


def _login_form(client):
    """Fetch the coach sign-in form's CSRF token/cookie, matching
    `tests/test_auth_api.py`'s own `_get_login_form` helper (not imported
    directly to avoid coupling this file to that one's fixtures)."""
    response = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1), {"bbbffl_csrf": response.cookies.get("bbbffl_csrf")}
