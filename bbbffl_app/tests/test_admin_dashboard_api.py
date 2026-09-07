"""HTTP-level authorization/security coverage for the Administrator
Dashboard (issue #148) -- proves the wiring in `app.main`/`app.routes.
admin_dashboard` (role boundary, season handling, discoverability,
responsive/accessible page shell, legacy-token-precedence warning without
secret disclosure) works end-to-end, the same way
`tests/test_scorer_dashboard_api.py` proves `app.routes.scorer_dashboard`'s
wiring rather than only `app.scorer_dashboard`'s own functions.
"""

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.authorization import Principal, Role
from tests.admin_dashboard_helpers import build_governed_season


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


def _seed(client, year, **kwargs):
    database = client.app.state.database
    g = build_governed_season(database, year=year, **kwargs)
    return g


def _override(client, dependency, principal):
    client.app.dependency_overrides[dependency] = lambda: principal


def _clear_overrides(client):
    client.app.dependency_overrides.clear()


# -- Administrator dashboard authentication ---------------------------------


def test_administrator_can_access_the_dashboard(dashboard_client):
    from app.routes.admin_dashboard import require_admin_dashboard

    client = dashboard_client
    g = _seed(client, 9401)
    principal = Principal(
        Role.ADMIN, "admin-1", "Standing Admin", granted_roles=frozenset({Role.ADMIN}), session_id="s1"
    )
    _override(client, require_admin_dashboard, principal)
    try:
        response = client.get("/api/admin/dashboard", params={"season_id": g.season.season_id})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dashboard"]["season"]["season_id"] == g.season.season_id
        assert body["selected_season_id"] == g.season.season_id
    finally:
        _clear_overrides(client)


def test_legacy_admin_token_can_access_the_dashboard(dashboard_client):
    client = dashboard_client
    _seed(client, 9402)
    response = client.get("/api/admin/dashboard")
    assert response.status_code == 200, response.text
    assert response.json()["acting_context"]["coach_id"] is None


def test_unauthenticated_request_is_rejected(monkeypatch):
    """Matching `test_scorer_dashboard_api.py`'s established convention:
    the shared `dashboard_client` fixture deletes `BBBFFL_ADMIN_TOKEN`
    entirely (an "open operator" dev/test posture), so proving a genuinely
    unauthenticated request is rejected needs a configured token."""
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "a-real-configured-token")
    monkeypatch.setenv("BBBFFL_SESSION_SECRET", "a-real-configured-session-secret")

    from app.main import app

    with TestClient(app) as client:
        _seed(client, 9403)
        response = client.get("/api/admin/dashboard")
        assert response.status_code == 401
    db_path.unlink(missing_ok=True)


# -- Coach / Secretary / Scorer / Replay Operator denial ---------------------


def test_coach_is_denied(dashboard_client):
    client = dashboard_client
    _seed(client, 9404)
    principal = Principal(Role.COACH, "coach-1", "Some Coach", session_id="s1")
    from app.authorization import resolve_principal

    _override(client, resolve_principal, principal)
    try:
        response = client.get("/api/admin/dashboard")
        assert response.status_code == 403
    finally:
        _clear_overrides(client)


def test_secretary_is_denied(dashboard_client):
    """Unlike the Scorer Dashboard (Scorer/Replay-Operator/Administrator),
    the Administrator Dashboard is strictly Administrator-only -- Secretary
    holds genuine ordinary season-setup authority elsewhere but must never
    reach Administrator-only identity/audit/configuration information here
    (issue #148's role boundary)."""
    from app.authorization import resolve_principal

    client = dashboard_client
    _seed(client, 9405)
    principal = Principal(
        Role.SECRETARY, "sec-1", "Some Secretary", granted_roles=frozenset({Role.SECRETARY}), session_id="s1"
    )
    _override(client, resolve_principal, principal)
    try:
        response = client.get("/api/admin/dashboard")
        assert response.status_code == 403
    finally:
        _clear_overrides(client)


def test_scorer_and_replay_operator_are_denied(dashboard_client):
    """Administrator-only -- unlike `/scorer`, holding Scorer or Replay
    Operator authority (even together with Scorer capability) never opens
    this dashboard's private governance data."""
    from app.authorization import resolve_principal

    client = dashboard_client
    _seed(client, 9406)
    for role in (Role.SCORER, Role.REPLAY_OPERATOR):
        principal = Principal(role, f"{role.value}-1", "Someone", granted_roles=frozenset({role}), session_id="s1")
        _override(client, resolve_principal, principal)
        try:
            response = client.get("/api/admin/dashboard")
            assert response.status_code == 403
        finally:
            _clear_overrides(client)


# -- Cross-season scope / browser-supplied identifiers -----------------------


def test_browser_supplied_unknown_season_id_never_confers_authority(dashboard_client):
    from app.routes.admin_dashboard import require_admin_dashboard

    client = dashboard_client
    _seed(client, 9407)
    principal = Principal(Role.ADMIN, "admin-1", "Admin", granted_roles=frozenset({Role.ADMIN}), session_id="s1")
    _override(client, require_admin_dashboard, principal)
    try:
        response = client.get("/api/admin/dashboard", params={"season_id": "does-not-exist"})
        assert response.status_code == 404
    finally:
        _clear_overrides(client)


def test_administrator_authority_is_never_season_scoped(dashboard_client):
    """Administrator grants cannot be season-scoped at all
    (`RoleGrantRepository.grant` refuses it) -- an Administrator must see
    every season, never only the one from an arbitrary request parameter."""
    from app.routes.admin_dashboard import require_admin_dashboard

    client = dashboard_client
    season_a = _seed(client, 9408).season.season_id
    season_b = _seed(client, 9409).season.season_id
    principal = Principal(Role.ADMIN, "admin-1", "Admin", granted_roles=frozenset({Role.ADMIN}), session_id="s1")
    _override(client, require_admin_dashboard, principal)
    try:
        for season_id in (season_a, season_b):
            response = client.get("/api/admin/dashboard", params={"season_id": season_id})
            assert response.status_code == 200, response.text
            assert response.json()["dashboard"]["season"]["season_id"] == season_id
        listed = client.get("/api/admin/dashboard").json()["portfolio"]
        assert {season_a, season_b} <= {row["season_id"] for row in listed}
    finally:
        _clear_overrides(client)


# -- Multiple-season portfolio / operational season != newest season --------


def test_multiple_season_portfolio_is_returned(dashboard_client):
    client = dashboard_client
    season_a = _seed(client, 9410).season.season_id
    season_b = _seed(client, 9411).season.season_id
    response = client.get("/api/admin/dashboard")
    assert response.status_code == 200
    years = {row["season_id"] for row in response.json()["portfolio"]}
    assert {season_a, season_b} <= years


def test_operational_season_need_not_be_the_newest_season(dashboard_client):
    client = dashboard_client
    active = _seed(client, 9412, close_preseason=True, open_round=True)
    _seed(client, 9413, entries=2)
    response = client.get("/api/admin/dashboard", params={"season_id": active.season.season_id})
    assert response.status_code == 200
    body = response.json()["dashboard"]
    assert body["current_round"]["state"] == "open"


# -- Private-state isolation --------------------------------------------


def test_private_role_grant_state_does_not_leak_between_seasons(dashboard_client):
    client = dashboard_client
    season_a = _seed(client, 9414).season.season_id
    _seed(client, 9415)
    coach_a = client.app.state.identities.create_coach("Season A Scorer", email="season-a-9414@example.com")
    client.app.state.role_grants.grant(
        coach_a.coach_id, Role.SCORER.value, season_id=season_a, actor=ActorContext.anonymous_operator("admin")
    )
    response = client.get("/api/admin/dashboard", params={"season_id": season_a})
    role_overview = response.json()["dashboard"]["role_overview"]
    assert role_overview["grants_by_role"]["scorer"][0]["coach_id"] == coach_a.coach_id


# -- Legacy-token precedence warning without secret disclosure --------------


def test_legacy_token_precedence_is_warned_without_leaking_the_token(monkeypatch):
    """Issue #148: when a request carries both a valid `X-Admin-Token` and
    an authenticated coach session cookie, `resolve_principal` silently
    prefers the token (see its own docstring) -- the dashboard must warn
    about this precedence without ever echoing the token value."""
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "super-secret-admin-token")
    monkeypatch.setenv("BBBFFL_SESSION_SECRET", "a-real-configured-session-secret")

    from app.main import app

    with TestClient(app) as client:
        password = "correct horse battery staple"  # noqa: S105 -- test fixture password
        coach = client.app.state.identities.create_coach("Dual Credential Admin", email="dual-9416@example.com")
        client.app.state.credentials.set_password(
            coach.coach_id, password, actor=ActorContext.anonymous_operator("admin")
        )
        client.app.state.role_grants.grant(
            coach.coach_id, Role.ADMIN.value, season_id=None, actor=ActorContext.anonymous_operator("admin")
        )
        login_page = client.get("/login")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
        login = client.post(
            "/login",
            data={"email": "dual-9416@example.com", "password": password, "csrf_token": csrf_match.group(1)},
            cookies=login_page.cookies,
            follow_redirects=False,
        )
        session_cookie = login.cookies.get("bbbffl_session")
        account_page = client.get("/account", cookies={"bbbffl_session": session_cookie})
        switch_csrf = _extract_csrf(account_page.text)
        switch_csrf_cookie = account_page.cookies.get("bbbffl_csrf")
        switch = client.post(
            "/api/context/role",
            json={"role": "admin"},
            cookies={"bbbffl_session": session_cookie, "bbbffl_csrf": switch_csrf_cookie},
            headers={"X-CSRF-Token": switch_csrf},
        )
        assert switch.status_code == 200, switch.text

        without_token = client.get("/api/admin/dashboard", cookies={"bbbffl_session": session_cookie})
        assert without_token.status_code == 200, without_token.text
        assert without_token.json()["authentication"]["legacy_token_precedence_warning"] is False
        assert without_token.json()["authentication"]["provenance"] == "authenticated_session"

        with_token = client.get(
            "/api/admin/dashboard",
            cookies={"bbbffl_session": session_cookie},
            headers={"X-Admin-Token": "super-secret-admin-token"},
        )
        body = with_token.json()
        assert body["authentication"]["legacy_token_precedence_warning"] is True
        assert body["authentication"]["provenance"] == "legacy_shared_token"
        assert "super-secret-admin-token" not in with_token.text
    db_path.unlink(missing_ok=True)


# -- Freshly logged-in Administrator discoverability/role switching ---------


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf_token hidden field not found in rendered page"
    return match.group(1)


def _login(client, *, email, password):
    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf_token},
        cookies=login_page.cookies,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.cookies.get("bbbffl_session")


def test_a_freshly_authenticated_granted_administrator_can_reach_the_dashboard_via_the_advertised_link(
    dashboard_client,
):
    """The same regression shape as #147's Codex P1 finding on PR #159,
    applied to the Administrator Dashboard: a coach identity holding a
    standing Administrator grant, whose session has not yet switched its
    active role away from "coach", must be able to reach
    `/api/admin/dashboard` by following exactly the sequence `/account`'s
    advertised button now performs -- without a 403 in between."""
    client = dashboard_client
    _seed(client, 9417)
    password = "correct horse battery staple"  # noqa: S105 -- test fixture password
    coach = client.app.state.identities.create_coach("Freshly Granted Admin", email="fresh-admin-9417@example.com")
    client.app.state.credentials.set_password(coach.coach_id, password, actor=ActorContext.anonymous_operator("admin"))
    client.app.state.role_grants.grant(
        coach.coach_id, Role.ADMIN.value, season_id=None, actor=ActorContext.anonymous_operator("admin")
    )
    session_cookie = _login(client, email="fresh-admin-9417@example.com", password=password)

    still_coach = client.get("/api/admin/dashboard", cookies={"bbbffl_session": session_cookie})
    assert still_coach.status_code == 403

    account_page = client.get("/account", cookies={"bbbffl_session": session_cookie})
    assert account_page.status_code == 200
    assert 'id="open-admin-dashboard"' in account_page.text
    assert '"admin"' in account_page.text
    csrf_token = _extract_csrf(account_page.text)
    csrf_cookie = account_page.cookies.get("bbbffl_csrf")

    switch = client.post(
        "/api/context/role",
        json={"role": "admin"},
        cookies={"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert switch.status_code == 200, switch.text
    now_admin = client.get("/api/admin/dashboard", cookies={"bbbffl_session": session_cookie})
    assert now_admin.status_code == 200, now_admin.text


def test_dual_role_administrator_can_navigate_to_the_scorer_dashboard_without_expanding_scorer_authority(
    dashboard_client,
):
    """Issue #148's "where Administrator also has Scorer capability,
    navigation to the Scorer Dashboard is permitted without expanding the
    underlying Scorer authority model" -- Administrator's own capability
    set already includes every Scorer capability (the wildcard in
    `app.authorization.CAPABILITIES`), so an Administrator active role can
    reach `/scorer` directly, with no separate Scorer grant required."""
    from app.routes.admin_dashboard import require_admin_dashboard
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboard_client
    g = _seed(client, 9418, close_preseason=True, open_round=True)
    principal = Principal(Role.ADMIN, "admin-1", "Admin", granted_roles=frozenset({Role.ADMIN}), session_id="s1")
    _override(client, require_admin_dashboard, principal)
    _override(client, require_scorer_dashboard, principal)
    try:
        admin_response = client.get("/api/admin/dashboard", params={"season_id": g.season.season_id})
        assert admin_response.status_code == 200
        scorer_response = client.get("/api/scorer/dashboard", params={"season_id": g.season.season_id})
        assert scorer_response.status_code == 200
    finally:
        _clear_overrides(client)


# -- Page shell: discoverable, responsive, accessible ------------------------


def test_admin_dashboard_page_renders_accessible_mobile_first_shell(dashboard_client):
    client = dashboard_client
    response = client.get("/admin/dashboard")
    assert response.status_code == 200
    html = response.text
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in html
    assert re.search(r"<h1>[^<]*Administrator Dashboard", html)
    assert 'aria-live="polite"' in html
    assert 'role="alert"' in html
    assert 'role="status"' in html
    assert "@media(max-width:640px)" in html
    assert "min-height:44px" in html


def test_legacy_admin_route_is_unaffected(dashboard_client):
    """Issue #148's explicit "resolve any conflict with the retained
    legacy Grand Final admin route" -- `GET /admin` must keep serving the
    pre-existing token-gated Grand Final panel untouched, and
    `/admin/dashboard` must be a genuinely separate route."""
    client = dashboard_client
    legacy = client.get("/admin")
    assert legacy.status_code == 200
    assert "Administrator Dashboard" not in legacy.text
    new_dashboard = client.get("/admin/dashboard")
    assert new_dashboard.status_code == 200
    assert "Administrator Dashboard" in new_dashboard.text
