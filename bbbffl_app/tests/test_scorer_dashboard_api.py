"""HTTP-level authorization coverage for the Scorer Operations Dashboard
(issue #147) -- proves the wiring in `app.main`/`app.routes.scorer_dashboard`
works end-to-end (role/season-scope enforcement, no private-state leakage),
the same way `tests/test_round_review_api.py` proves `app.routes.round_review`'s
wiring rather than only `app.round_review`'s own functions.
"""

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.authorization import Principal, Role
from tests.round_review_helpers import Facts, full_round


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


def _seed(client, year):
    database = client.app.state.database
    db, lifecycle, round_, entries, stats, canon = full_round(database, year=year)
    client.app.state.afl_client = Facts(stats)
    season_id = lifecycle.get_round(round_.bbbffl_round_id).season_id
    return season_id, round_, entries


def _override(client, dependency, principal):
    client.app.dependency_overrides[dependency] = lambda: principal


def _clear_overrides(client):
    client.app.dependency_overrides.clear()


def test_scorer_can_access_the_dashboard(dashboard_client):
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboard_client
    season_id, round_, entries = _seed(client, 9101)
    scorer = client.app.state.identities.create_coach("Standing Scorer", email="standing-9101@example.com")
    client.app.state.role_grants.grant(
        scorer.coach_id, Role.SCORER.value, season_id=None, actor=ActorContext.anonymous_operator("admin")
    )
    principal = Principal(
        Role.SCORER, scorer.coach_id, "Standing Scorer", granted_roles=frozenset({Role.SCORER}), session_id="s1"
    )
    _override(client, require_scorer_dashboard, principal)
    try:
        response = client.get("/api/scorer/dashboard", params={"season_id": season_id})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dashboard"]["season"]["season_id"] == season_id
        assert len(body["dashboard"]["lineups"]) == 10
    finally:
        _clear_overrides(client)


def test_coach_is_denied(dashboard_client):
    client = dashboard_client
    _seed(client, 9102)
    principal = Principal(Role.COACH, "coach-1", "Some Coach", session_id="s1")
    client.app.dependency_overrides = {}
    from app.authorization import resolve_principal

    _override(client, resolve_principal, principal)
    try:
        response = client.get("/api/scorer/dashboard")
        assert response.status_code == 403
    finally:
        _clear_overrides(client)


def test_admin_can_access_any_season(dashboard_client):
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboard_client
    season_id, round_, entries = _seed(client, 9103)
    principal = Principal(Role.ADMIN, "admin-1", "Admin", session_id="s1")
    _override(client, require_scorer_dashboard, principal)
    try:
        response = client.get("/api/scorer/dashboard", params={"season_id": season_id})
        assert response.status_code == 200, response.text
    finally:
        _clear_overrides(client)


def test_replay_operator_scoped_to_season_grant_is_denied_other_seasons(dashboard_client):
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboard_client
    season_a, _, _ = _seed(client, 9104)
    season_b, _, _ = _seed(client, 9105)
    operator = client.app.state.identities.create_coach("Replay Operator", email="replay-op-9104@example.com")
    client.app.state.role_grants.grant(
        operator.coach_id,
        Role.REPLAY_OPERATOR.value,
        season_id=season_a,
        actor=ActorContext.anonymous_operator("admin"),
    )
    principal = Principal(
        Role.REPLAY_OPERATOR,
        operator.coach_id,
        "Replay Operator",
        granted_roles=frozenset({Role.REPLAY_OPERATOR}),
        session_id="s1",
    )
    _override(client, require_scorer_dashboard, principal)
    try:
        allowed = client.get("/api/scorer/dashboard", params={"season_id": season_a})
        assert allowed.status_code == 200, allowed.text
        denied = client.get("/api/scorer/dashboard", params={"season_id": season_b})
        assert denied.status_code == 403
    finally:
        _clear_overrides(client)


def test_multiple_season_grants_are_all_listed_and_selectable(dashboard_client):
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboard_client
    season_a, round_a, _ = _seed(client, 9106)
    season_b, round_b, _ = _seed(client, 9107)
    scorer = client.app.state.identities.create_coach("Multi Season Scorer", email="multi-9106@example.com")
    for season_id in (season_a, season_b):
        client.app.state.role_grants.grant(
            scorer.coach_id, Role.SCORER.value, season_id=season_id, actor=ActorContext.anonymous_operator("admin")
        )
    principal = Principal(
        Role.SCORER, scorer.coach_id, "Multi Season Scorer", granted_roles=frozenset({Role.SCORER}), session_id="s1"
    )
    _override(client, require_scorer_dashboard, principal)
    try:
        response = client.get("/api/scorer/dashboard")
        assert response.status_code == 200, response.text
        body = response.json()
        listed_seasons = {season["season_id"] for season in body["seasons"]}
        assert {season_a, season_b} <= listed_seasons
        explicit = client.get("/api/scorer/dashboard", params={"season_id": season_b})
        assert explicit.status_code == 200
        assert explicit.json()["dashboard"]["season"]["season_id"] == season_b
    finally:
        _clear_overrides(client)


def test_season_scoped_scorer_cannot_reach_an_unauthorised_season(dashboard_client):
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboard_client
    season_a, _, _ = _seed(client, 9108)
    season_b, _, _ = _seed(client, 9109)
    scorer = client.app.state.identities.create_coach("Scoped Scorer", email="scoped-9108@example.com")
    client.app.state.role_grants.grant(
        scorer.coach_id, Role.SCORER.value, season_id=season_a, actor=ActorContext.anonymous_operator("admin")
    )
    principal = Principal(
        Role.SCORER, scorer.coach_id, "Scoped Scorer", granted_roles=frozenset({Role.SCORER}), session_id="s1"
    )
    _override(client, require_scorer_dashboard, principal)
    try:
        response = client.get("/api/scorer/dashboard")
        body = response.json()
        assert {season["season_id"] for season in body["seasons"]} == {season_a}
        assert body["dashboard"]["season"]["season_id"] == season_a
        denied = client.get("/api/scorer/dashboard", params={"season_id": season_b})
        assert denied.status_code == 403
    finally:
        _clear_overrides(client)


def test_private_team_state_does_not_leak_between_seasons(dashboard_client):
    """A scorer scoped to season A must never see season B's team names or
    lineup readiness rows in the same response, even implicitly."""
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboard_client
    season_a, round_a, entries_a = _seed(client, 9110)
    season_b, round_b, entries_b = _seed(client, 9111)
    scorer = client.app.state.identities.create_coach("Isolated Scorer", email="isolated-9110@example.com")
    client.app.state.role_grants.grant(
        scorer.coach_id, Role.SCORER.value, season_id=season_a, actor=ActorContext.anonymous_operator("admin")
    )
    principal = Principal(
        Role.SCORER, scorer.coach_id, "Isolated Scorer", granted_roles=frozenset({Role.SCORER}), session_id="s1"
    )
    _override(client, require_scorer_dashboard, principal)
    try:
        response = client.get("/api/scorer/dashboard", params={"season_id": season_a})
        body = response.json()
        entry_ids_a = {row["season_entry_id"] for row in body["dashboard"]["lineups"]}
        entry_ids_b = {entry.season_entry_id for entry in entries_b}
        assert entry_ids_a.isdisjoint(entry_ids_b)
    finally:
        _clear_overrides(client)


def test_unauthenticated_request_is_rejected(monkeypatch):
    """The shared `dashboard_client` fixture deletes `BBBFFL_ADMIN_TOKEN`
    entirely, matching every other API test module's "open operator" dev/
    test convention (`resolve_principal` then treats *any* credential-free
    request as legacy Administrator authority) -- so proving a genuinely
    unauthenticated request is rejected needs a *configured* token instead,
    the same way production always runs."""
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "a-real-configured-token")
    monkeypatch.setenv("BBBFFL_SESSION_SECRET", "a-real-configured-session-secret")

    from app.main import app

    with TestClient(app) as client:
        _seed(client, 9112)
        response = client.get("/api/scorer/dashboard")
        assert response.status_code == 401
    db_path.unlink(missing_ok=True)


def test_scorer_home_page_renders_accessible_mobile_first_shell(dashboard_client):
    """Representative responsive/accessibility coverage (this suite's
    established convention for page shells -- see
    `tests/test_round_centre_selector_refresh.py`'s extraction of the real
    rendered template): the actual served page must declare a mobile
    viewport, a semantic document heading, an ARIA live region for
    asynchronous updates, an ARIA alert region for errors, and a narrow-
    viewport media query -- not colour alone."""
    client = dashboard_client
    response = client.get("/scorer")
    assert response.status_code == 200
    html = response.text
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in html
    assert re.search(r"<h1>[^<]*Scorer Operations Dashboard", html)
    assert 'aria-live="polite"' in html
    assert 'role="alert"' in html
    assert 'role="status"' in html
    assert "@media(max-width:640px)" in html
    assert "min-height:44px" in html


def test_scorer_dashboard_alias_redirects_to_canonical_route(dashboard_client):
    client = dashboard_client
    response = client.get("/scorer/dashboard", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/scorer"


def test_next_action_marks_actionability_by_the_active_roles_own_capabilities(dashboard_client):
    """Codex review, PR #159: a Replay Operator is admitted to this
    dashboard but does not hold `roundsetup.manage` -- the round preflight
    capability an unconfigured-lockout next action needs. The dashboard
    must say so rather than advertise a link that always 403s for that
    role, while a Scorer (who does hold it) sees the same action as
    actionable."""
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboard_client
    # `full_round` opens the round without configuring any lockout trigger
    # plan, so the deterministic next action is "configure_lockout_plan"
    # (capability `roundsetup.manage`) -- Role.REPLAY_OPERATOR's capability
    # set does not include it (app.authorization.CAPABILITIES).
    season_id, round_, entries = _seed(client, 9113)
    replay_op_coach = client.app.state.identities.create_coach("Replay Operator", email="replay-9113@example.com")
    client.app.state.role_grants.grant(
        replay_op_coach.coach_id,
        Role.REPLAY_OPERATOR.value,
        season_id=season_id,
        actor=ActorContext.anonymous_operator("admin"),
    )
    scorer_coach = client.app.state.identities.create_coach("Scorer", email="scorer-9113@example.com")
    client.app.state.role_grants.grant(
        scorer_coach.coach_id, Role.SCORER.value, season_id=None, actor=ActorContext.anonymous_operator("admin")
    )

    replay_operator = Principal(
        Role.REPLAY_OPERATOR,
        replay_op_coach.coach_id,
        "Replay Operator",
        granted_roles=frozenset({Role.REPLAY_OPERATOR}),
        session_id="s1",
    )
    _override(client, require_scorer_dashboard, replay_operator)
    try:
        response = client.get("/api/scorer/dashboard", params={"season_id": season_id})
        assert response.status_code == 200, response.text
        next_action = response.json()["dashboard"]["next_action"]
        assert next_action["code"] == "configure_lockout_plan"
        assert next_action["capability"] == "roundsetup.manage"
        assert next_action["actionable_by_you"] is False
    finally:
        _clear_overrides(client)

    scorer = Principal(
        Role.SCORER, scorer_coach.coach_id, "Scorer", granted_roles=frozenset({Role.SCORER}), session_id="s1"
    )
    _override(client, require_scorer_dashboard, scorer)
    try:
        response = client.get("/api/scorer/dashboard", params={"season_id": season_id})
        assert response.status_code == 200, response.text
        next_action = response.json()["dashboard"]["next_action"]
        assert next_action["code"] == "configure_lockout_plan"
        assert next_action["actionable_by_you"] is True
    finally:
        _clear_overrides(client)


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


def test_a_freshly_authenticated_granted_scorer_can_reach_the_dashboard_via_the_advertised_link(dashboard_client):
    """End-to-end regression for Codex's P1 finding on PR #159: a coach
    identity holding a standing Scorer grant, but whose session has not
    yet switched its *active* role away from the "coach" every login
    starts as, must be able to reach `/api/scorer/dashboard` by following
    exactly the sequence `/account`'s advertised button now performs --
    without a 403 in between."""
    client = dashboard_client
    _seed(client, 9114)
    password = "correct horse battery staple"  # noqa: S105 -- test fixture password, not a real credential
    coach = client.app.state.identities.create_coach("Freshly Granted Scorer", email="fresh-scorer-9114@example.com")
    client.app.state.credentials.set_password(coach.coach_id, password, actor=ActorContext.anonymous_operator("admin"))
    client.app.state.role_grants.grant(
        coach.coach_id, Role.SCORER.value, season_id=None, actor=ActorContext.anonymous_operator("admin")
    )
    session_cookie = _login(client, email="fresh-scorer-9114@example.com", password=password)

    # A brand-new session's active role is always "coach" (app.auth.
    # ActingContextService), so the dashboard API must still 403 here --
    # this is the exact failure Codex flagged, reproduced before the fix
    # is exercised.
    still_coach = client.get("/api/scorer/dashboard", cookies={"bbbffl_session": session_cookie})
    assert still_coach.status_code == 403

    account_page = client.get("/account", cookies={"bbbffl_session": session_cookie})
    assert account_page.status_code == 200
    assert 'id="open-scorer-dashboard"' in account_page.text
    assert '"scorer"' in account_page.text
    csrf_token = _extract_csrf(account_page.text)
    csrf_cookie = account_page.cookies.get("bbbffl_csrf")

    # Exactly what the account page's button now does: activate the
    # granted role, then reach the dashboard.
    switch = client.post(
        "/api/context/role",
        json={"role": "scorer"},
        cookies={"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert switch.status_code == 200, switch.text
    now_scorer = client.get("/api/scorer/dashboard", cookies={"bbbffl_session": session_cookie})
    assert now_scorer.status_code == 200, now_scorer.text
