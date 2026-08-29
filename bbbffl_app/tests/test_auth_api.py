"""End-to-end coach authentication coverage through FastAPI's TestClient
(roadmap package 19, issue #74): the full browser sign-in/sign-out flow,
CSRF protection, rate limiting, cookie security flags, credential/session
material never leaking through responses, and that scorer/admin proxy
provenance is unaffected by any of it.
"""

import dataclasses
import os
import re

import pytest
from fastapi.testclient import TestClient

from app.afl_client import Match, Player
from app.audit import ActorContext
from app.service import PlayerIdentityCache
from tests.conftest import CATS, PIES, TEAM_A_ROSTER, TEAM_B_ROSTER, FakeAflClient

GRAND_FINAL_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "grand_final_teams.json")
PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BBBFFL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AFL_API_BASE_URL", "http://unused.invalid")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "secret123")
    monkeypatch.setenv("BBBFFL_TEAMS_CONFIG_PATH", GRAND_FINAL_CONFIG_PATH)

    from app.main import app

    with TestClient(app) as test_client:
        # Same FakeAflClient swap tests/test_api.py's `client` fixture uses,
        # so the admin routes this file exercises (which build a live
        # matchup view) don't need real afl-api/network access.
        players = {}
        for ids, team in ((TEAM_A_ROSTER.values(), CATS), (TEAM_B_ROSTER.values(), PIES)):
            for pid in ids:
                players[pid] = Player(canonical_player_id=pid, name=f"Player {pid}", current_team=team)
        for team in app.state.teams:
            for pid in team.roster.values():
                players.setdefault(pid, Player(canonical_player_id=pid, name=f"Player {pid}", current_team=CATS))
        match = Match(match_id=100, home_team=CATS, away_team=PIES, status="LIVE")
        fake = FakeAflClient([match], players, {100: {}})
        app.state.afl_client = fake
        app.state.identity_cache = PlayerIdentityCache(fake)
        yield test_client


def _register_coach(app, email="coach@example.com", name="Test Coach", password=PASSWORD):
    coach = app.state.identities.create_coach(name, email=email)
    app.state.credentials.set_password(coach.coach_id, password, actor=ActorContext.anonymous_operator("admin"))
    return coach


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf_token hidden field not found in rendered page"
    return match.group(1)


def _get_login_form(client):
    response = client.get("/login")
    assert response.status_code == 200
    return _extract_csrf(response.text), response.cookies


# -- Full sign-in / sign-out flow --------------------------------------------


def test_full_sign_in_sign_out_flow(client):
    from app.main import app

    _register_coach(app)
    csrf_token, cookies = _get_login_form(client)

    login_response = client.post(
        "/login",
        data={"email": "coach@example.com", "password": PASSWORD, "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/account"
    session_cookie = login_response.cookies.get("bbbffl_session")
    assert session_cookie

    account_response = client.get("/account", cookies={"bbbffl_session": session_cookie})
    assert account_response.status_code == 200
    assert "Test Coach" in account_response.text

    account_csrf = _extract_csrf(account_response.text)
    logout_response = client.post(
        "/logout",
        data={"csrf_token": account_csrf},
        cookies={"bbbffl_session": session_cookie, "bbbffl_csrf": account_response.cookies.get("bbbffl_csrf")},
        follow_redirects=False,
    )
    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == "/login"

    after_logout = client.get("/account", cookies={"bbbffl_session": session_cookie})
    assert after_logout.status_code in (303, 200)  # TestClient follows redirects by default here
    assert "Test Coach" not in after_logout.text


def test_unauthenticated_account_page_redirects_to_login(client):
    response = client.get("/account", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_already_authenticated_login_page_redirects_to_account(client):
    from app.main import app

    _register_coach(app)
    csrf_token, cookies = _get_login_form(client)
    login_response = client.post(
        "/login",
        data={"email": "coach@example.com", "password": PASSWORD, "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    session_cookie = login_response.cookies.get("bbbffl_session")

    response = client.get("/login", cookies={"bbbffl_session": session_cookie}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/account"


# -- CSRF protection ----------------------------------------------------------


def test_login_without_a_csrf_token_is_rejected(client):
    from app.main import app

    _register_coach(app)
    _, cookies = _get_login_form(client)

    response = client.post(
        "/login", data={"email": "coach@example.com", "password": PASSWORD}, cookies=cookies, follow_redirects=False
    )

    assert response.status_code == 403
    assert "bbbffl_session" not in response.cookies


def test_login_with_a_csrf_token_not_matching_the_cookie_is_rejected(client):
    """The double-submit defence: a submitted token that doesn't match the
    cookie (as a cross-site attacker, unable to read the cookie, would be
    forced to either omit or guess) must be rejected."""
    from app.main import app

    _register_coach(app)
    csrf_token, cookies = _get_login_form(client)

    response = client.post(
        "/login",
        data={"email": "coach@example.com", "password": PASSWORD, "csrf_token": csrf_token + "-tampered"},
        cookies=cookies,
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "bbbffl_session" not in response.cookies


def test_login_with_no_csrf_cookie_at_all_is_rejected(client):
    """A request that never fetched the login page (no CSRF cookie set)
    but somehow guesses a plausible-looking token must still fail."""
    from app.main import app

    _register_coach(app)

    response = client.post(
        "/login",
        data={"email": "coach@example.com", "password": PASSWORD, "csrf_token": "guessed-token"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "bbbffl_session" not in response.cookies


def test_logout_without_a_valid_csrf_token_does_not_revoke_the_session(client):
    from app.main import app

    _register_coach(app)
    csrf_token, cookies = _get_login_form(client)
    login_response = client.post(
        "/login",
        data={"email": "coach@example.com", "password": PASSWORD, "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    session_cookie = login_response.cookies.get("bbbffl_session")

    bad_logout = client.post(
        "/logout",
        data={"csrf_token": "not-the-real-token"},
        cookies={"bbbffl_session": session_cookie},
        follow_redirects=False,
    )
    assert bad_logout.status_code == 303  # redirected back, but nothing was revoked

    still_valid = client.get("/account", cookies={"bbbffl_session": session_cookie})
    assert "Test Coach" in still_valid.text


# -- Rate limiting --------------------------------------------------------


def test_repeated_failed_logins_are_rate_limited(client):
    from app.main import app

    _register_coach(app)
    app.state.login_rate_limiter._max_attempts = 3  # keep the test fast/deterministic

    statuses = []
    for _ in range(4):
        csrf_token, cookies = _get_login_form(client)
        response = client.post(
            "/login",
            data={"email": "coach@example.com", "password": "wrong-password", "csrf_token": csrf_token},
            cookies=cookies,
            follow_redirects=False,
        )
        statuses.append(response.status_code)

    assert statuses[:3] == [401, 401, 401]
    assert statuses[3] == 429


# -- Cookie security flags -----------------------------------------------


def test_development_cookies_are_not_marked_secure(client):
    response = client.get("/login")
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Secure" not in set_cookie
    assert "samesite=lax" in set_cookie.lower()


def test_production_cookies_are_marked_secure(client):
    """`Secure` follows `settings.is_production` -- simulated here by
    swapping app.state.settings for a production-flagged copy after
    startup, since a real production deployment requires PostgreSQL."""
    from app.main import app

    app.state.settings = dataclasses.replace(app.state.settings, environment="production")

    response = client.get("/login")
    set_cookie = response.headers.get("set-cookie", "")
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie


def test_session_cookie_is_httponly_and_bounded_lifetime(client):
    from app.main import app

    _register_coach(app)
    csrf_token, cookies = _get_login_form(client)
    login_response = client.post(
        "/login",
        data={"email": "coach@example.com", "password": PASSWORD, "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    set_cookie = login_response.headers.get("set-cookie", "")
    assert "bbbffl_session" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Max-Age=" in set_cookie


# -- No credential/token leakage -------------------------------------------


def test_failed_login_never_echoes_the_submitted_password(client):
    from app.main import app

    _register_coach(app)
    csrf_token, cookies = _get_login_form(client)
    response = client.post(
        "/login",
        data={"email": "coach@example.com", "password": "super-secret-guess", "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    assert "super-secret-guess" not in response.text


def test_successful_login_redirect_never_carries_the_session_token_in_the_url(client):
    from app.main import app

    _register_coach(app)
    csrf_token, cookies = _get_login_form(client)
    response = client.post(
        "/login",
        data={"email": "coach@example.com", "password": PASSWORD, "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    assert response.headers["location"] == "/account"  # no query string, no token


# -- One coach cannot use another's session ----------------------------------


def test_one_coachs_session_cookie_never_resolves_to_another_coach(client):
    from app.main import app

    _register_coach(app, email="a@example.com", name="Coach A")
    _register_coach(app, email="b@example.com", name="Coach B")

    csrf_a, cookies_a = _get_login_form(client)
    login_a = client.post(
        "/login",
        data={"email": "a@example.com", "password": PASSWORD, "csrf_token": csrf_a},
        cookies=cookies_a,
        follow_redirects=False,
    )
    session_a = login_a.cookies.get("bbbffl_session")

    page = client.get("/account", cookies={"bbbffl_session": session_a})
    assert "Coach A" in page.text
    assert "Coach B" not in page.text


# -- Navigation ---------------------------------------------------------------


def test_public_page_shows_sign_in_link_when_unauthenticated(client):
    response = client.get("/")
    assert "Coach sign in" in response.text


def test_public_page_shows_signed_in_state_after_login(client):
    from app.main import app

    _register_coach(app)
    csrf_token, cookies = _get_login_form(client)
    login_response = client.post(
        "/login",
        data={"email": "coach@example.com", "password": PASSWORD, "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    session_cookie = login_response.cookies.get("bbbffl_session")

    response = client.get("/", cookies={"bbbffl_session": session_cookie})
    assert "Signed in as Test Coach" in response.text


# -- Scorer/admin proxy provenance is unaffected -----------------------------


ADMIN_HEADERS = {"X-Admin-Token": "secret123"}


def test_admin_routes_are_unaffected_by_coach_authentication(client):
    """The existing shared-token scorer/admin surface (app/routes/admin.py)
    is untouched by this package -- an admin action needs only the admin
    token, never a coach session, and a coach session grants no admin
    access."""
    from app.main import app

    _register_coach(app)
    csrf_token, cookies = _get_login_form(client)
    login_response = client.post(
        "/login",
        data={"email": "coach@example.com", "password": PASSWORD, "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    session_cookie = login_response.cookies.get("bbbffl_session")

    # A coach session alone grants no admin access.
    unauthorized = client.get("/api/admin/state", cookies={"bbbffl_session": session_cookie})
    # Issue #75 makes the distinction deliberate: this is a valid coach
    # principal lacking operator capability, not a missing credential.
    assert unauthorized.status_code == 403

    # The admin token alone still works, with no coach session at all.
    authorized = client.get("/api/admin/state", headers=ADMIN_HEADERS)
    assert authorized.status_code == 200


# -- Admin-assisted recovery/re-entry ----------------------------------------


def test_admin_can_reset_a_coachs_password_by_coach_id(client):
    from app.main import app

    coach = _register_coach(app, password="original-password-123")

    response = client.post(
        "/api/admin/coach-credential",
        json={"coach_id": coach.coach_id, "password": "brand-new-password-456"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200

    csrf_token, cookies = _get_login_form(client)
    login = client.post(
        "/login",
        data={"email": "coach@example.com", "password": "brand-new-password-456", "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.cookies.get("bbbffl_session")


def test_admin_can_reset_a_coachs_password_by_email(client):
    from app.main import app

    _register_coach(app, password="original-password-123")

    response = client.post(
        "/api/admin/coach-credential",
        json={"email": "coach@example.com", "password": "brand-new-password-456"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["coach_id"]


def test_admin_credential_reset_requires_the_admin_token(client):
    from app.main import app

    coach = _register_coach(app)

    response = client.post(
        "/api/admin/coach-credential",
        json={"coach_id": coach.coach_id, "password": "brand-new-password-456"},
    )
    assert response.status_code == 401


def test_admin_credential_reset_rejects_a_weak_password(client):
    from app.main import app

    coach = _register_coach(app)

    response = client.post(
        "/api/admin/coach-credential",
        json={"coach_id": coach.coach_id, "password": "short"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 400


def test_admin_credential_reset_for_unknown_email_returns_404(client):
    response = client.post(
        "/api/admin/coach-credential",
        json={"email": "nobody@example.com", "password": "brand-new-password-456"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 404


def test_admin_credential_reset_for_unknown_coach_id_returns_404_not_500(client):
    """Regression: a mistyped/deleted coach_id must be rejected before the
    credential insert hits the coach_id foreign key, not surface as an
    uncaught IntegrityError/500."""
    response = client.post(
        "/api/admin/coach-credential",
        json={"coach_id": "does-not-exist", "password": "brand-new-password-456"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 404


def test_admin_credential_reset_revokes_the_coachs_existing_sessions(client):
    """Regression: resetting a password (e.g. after a suspected compromise)
    must invalidate any session token issued before the reset -- otherwise
    a stolen cookie keeps working for the rest of its lifetime regardless
    of the reset."""
    from app.main import app

    coach = _register_coach(app, password="original-password-123")
    csrf_token, cookies = _get_login_form(client)
    login = client.post(
        "/login",
        data={"email": "coach@example.com", "password": "original-password-123", "csrf_token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    session_cookie = login.cookies.get("bbbffl_session")
    assert "Test Coach" in client.get("/account", cookies={"bbbffl_session": session_cookie}).text

    reset = client.post(
        "/api/admin/coach-credential",
        json={"coach_id": coach.coach_id, "password": "brand-new-password-456"},
        headers=ADMIN_HEADERS,
    )
    assert reset.status_code == 200

    after_reset = client.get("/account", cookies={"bbbffl_session": session_cookie}, follow_redirects=False)
    assert after_reset.status_code == 303  # no longer authenticated -- redirected to /login


def test_admin_dnp_action_is_still_attributed_to_the_anonymous_operator_not_a_coach(client):
    """Proves app.audit's actor/provenance boundary is unchanged: an admin
    proxy mutation is still `anonymous_operator`, never `coach`, even
    though `coach` is now a valid actor type elsewhere in the system (see
    app/lineup_proxy.py's module docstring, "Actor, never the coach")."""
    from app.main import app

    response = client.post(
        "/api/admin/dnp",
        json={"team_key": "team_a", "slot": "Forward1", "dnp": True},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200

    events = app.state.audit_events.list_events(entity_type="scoring.slot")
    assert events
    assert events[-1].actor_type == "anonymous_operator"
    assert events[-1].actor_type != "coach"
