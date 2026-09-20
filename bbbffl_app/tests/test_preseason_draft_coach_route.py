"""Issue #229: the Coach-facing pre-season draft surface at
`/account/preseason-draft/{season_id}` -- discoverability from `/account`,
read-only behaviour while another team is on the clock, a genuine Coach
self-service pick with Coach audit provenance, cross-team rejection, and
confirmation that operator-only controls remain on `/admin/draft/...` only.

Mirrors `tests/test_midseason_draft_board_api.py`'s coach-route coverage and
`tests/test_draft_authenticated_actor.py`'s minimal draft-ready fixture --
the preseason draft needs no ladder/competition/delisting setup, only an
accepted order (`DraftRepository.accept_order`).
"""

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext, AuditEventRepository
from app.identity import IdentityRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.season import SeasonRepository

PASSWORD = "correct horse battery staple"
ENTRIES = 3
SQUAD_LIMIT = 2


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


def _login(client, *, email, password=PASSWORD):
    # `TestClient` persists cookies across calls on the same instance; when a
    # test logs in as a second coach, an already-present session cookie
    # would make `GET /login` redirect straight to `/account` instead of
    # rendering the login form (and CSRF token) this helper needs -- clear
    # the jar first so every call starts from a clean, unauthenticated state.
    client.cookies.clear()
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


def _seed_draft_ready_season(database, *, year, label):
    """A minimal accepted/frozen preseason draft with a real current pick --
    trimmed to exactly what `DraftRepository.accept_order` needs, mirroring
    `tests/test_draft_authenticated_actor.py::_seed_draft_ready_season`.
    Three entries, squad limit 2 (six total picks, snake order), so the
    round-1/round-3 order differs from round 2 -- enough to exercise "not my
    turn yet" before a coach's own pick arrives."""
    season = SeasonRepository(database).create_season(year, label)
    identities = IdentityRepository(database)
    entries = [
        identities.create_entry(
            season.season_id,
            f"entry-{number}",
            identities.create_coach(
                f"Team Coach {number}", email=f"preseason-coach-{number}-{year}@example.com"
            ).coach_id,
            f"Team {number}",
        )
        for number in range(ENTRIES)
    ]
    OwnershipRepository(database).configure_squad_limit(season.season_id, SQUAD_LIMIT)
    pool = PlayerPoolRepository(database)
    players = [
        pool.refresh_player(season.season_id, 4000 + number, f"Draftable Player {number}") for number in range(12)
    ]

    from app.draft import DraftRepository

    DraftRepository(database).accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    return season, entries, players


def _give_credentials(app, season_entry_id, *, password=PASSWORD):
    """`accept_order` only takes `SeasonEntry` rows (no `coach_id` field) --
    resolve the owning coach the same way `app.draft_board.entry_view` and
    the mid-season coach tests do, via `IdentityRepository.get_current_coach`."""
    coach = app.state.identities.get_current_coach(season_entry_id)
    app.state.credentials.set_password(coach.coach_id, password, actor=ActorContext.anonymous_operator("admin"))
    return coach


def test_account_shows_preseason_draft_cue_when_relevant_and_hides_it_once_finalized(client):
    database = client.app.state.database
    season, entries, players = _seed_draft_ready_season(database, year=4001, label="Preseason Coach Regression")
    on_the_clock = entries[0]
    _give_credentials(client.app, on_the_clock.season_entry_id)
    session = _login(client, email="preseason-coach-0-4001@example.com")
    cookies = {"bbbffl_session": session}

    account = client.get("/account", cookies=cookies)
    assert account.status_code == 200, account.text
    assert f"/account/preseason-draft/{season.season_id}" in account.text
    assert "Pre-season draft selections" in account.text
    assert "Team 0" in account.text
    assert "follow the draft, manage your shortlist, and select when it is your turn" in account.text

    # Finalising requires completing every pick first (fail-closed domain
    # rule) -- complete all six via the operator proxy surface, then
    # finalize. `TestClient` persists cookies across requests on the same
    # client, so these operator calls must explicitly override the coach
    # session cookie to empty (matching test_midseason_draft_board_api's
    # `cookies={"bbbffl_session": ""}` pattern) -- an empty token resolves
    # to no session, which defaults to admin in dev/test mode.
    no_session = {"bbbffl_session": ""}
    admin_api = f"/api/admin/draft/{season.season_id}"
    for _ in range(ENTRIES * SQUAD_LIMIT):
        board = client.get(f"{admin_api}/board", cookies=no_session).json()
        current = board["current_pick"]
        available = client.get(f"{admin_api}/available-players", cookies=no_session).json()
        response = client.post(
            f"{admin_api}/pick",
            json={
                "season_entry_id": current["current_season_entry_id"],
                "season_player_id": available[0]["season_player_id"],
                "draft_pick_id": current["draft_pick_id"],
                "scorer_name": "Scorer Sam",
            },
            cookies=no_session,
        )
        assert response.status_code == 200, response.text
    finalize = client.post(f"{admin_api}/finalize", json={}, cookies=no_session)
    assert finalize.status_code == 200, finalize.text

    account_after = client.get("/account", cookies=cookies)
    assert account_after.status_code == 200, account_after.text
    assert f"/account/preseason-draft/{season.season_id}" not in account_after.text
    assert "Pre-season draft selections" not in account_after.text


def test_coach_route_is_reachable_without_admin_role_and_read_only_until_own_turn(client):
    client_ = client
    database = client_.app.state.database
    season, entries, players = _seed_draft_ready_season(database, year=4002, label="Preseason Coach Read Only")
    second_pick_entry = entries[1]
    _give_credentials(client_.app, second_pick_entry.season_entry_id)
    session = _login(client_, email="preseason-coach-1-4002@example.com")
    cookies = {"bbbffl_session": session}

    coach_page = client_.get(f"/account/preseason-draft/{season.season_id}", cookies=cookies)
    assert coach_page.status_code == 200, coach_page.text
    assert "BBBFFL Team 1 Preseason Draft" in coach_page.text
    assert "/api/account/preseason-draft" in coach_page.text
    assert "Admin token" not in coach_page.text
    assert "Pre-draft readiness" not in coach_page.text
    assert "Proxy provenance" not in coach_page.text
    assert "Pause / resume" not in coach_page.text
    assert "Finalise draft" not in coach_page.text
    assert "Danger zone" not in coach_page.text
    assert f'href="/admin/draft/{season.season_id}"' not in coach_page.text
    assert "Read-only until it is your team’s turn." in coach_page.text

    coach_api = f"/api/account/preseason-draft/{season.season_id}"
    board = client_.get(f"{coach_api}/board", cookies=cookies).json()
    # Entry 0 picks first in round 1 -- entry 1 (this coach) is not yet on
    # the clock.
    assert board["current_pick"]["current_season_entry_id"] == entries[0].season_entry_id
    assert "delistings" not in board
    assert "readiness" in board  # board still carries its own readiness section; no separate operator block rendered


def test_coach_self_service_pick_has_coach_audit_provenance_and_cross_team_pick_is_rejected(client):
    database = client.app.state.database
    season, entries, players = _seed_draft_ready_season(database, year=4003, label="Preseason Coach Self Service")
    first_pick_entry = entries[0]
    other_entry = entries[1]
    first_pick_coach = _give_credentials(client.app, first_pick_entry.season_entry_id)
    _give_credentials(client.app, other_entry.season_entry_id)

    own_session = _login(client, email="preseason-coach-0-4003@example.com")
    own_cookies = {"bbbffl_session": own_session}
    coach_api = f"/api/account/preseason-draft/{season.season_id}"

    board = client.get(f"{coach_api}/board", cookies=own_cookies).json()
    current_pick = board["current_pick"]
    assert current_pick["current_season_entry_id"] == first_pick_entry.season_entry_id

    players_resp = client.get(f"{coach_api}/players", cookies=own_cookies, params={"availability": "available"}).json()
    assert players_resp
    assert all(item["stats_label"] == "Previous season" for item in players_resp)
    chosen = players_resp[0]

    response = client.post(
        f"{coach_api}/pick",
        json={
            "season_entry_id": first_pick_entry.season_entry_id,
            "season_player_id": chosen["season_player_id"],
            "draft_pick_id": current_pick["draft_pick_id"],
        },
        cookies=own_cookies,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    completed = next(p for p in body["completed_picks"] if p["draft_pick_id"] == current_pick["draft_pick_id"])
    assert completed["proxy"] is None

    events = AuditEventRepository(database).list_events(action="draft.pick.completed")
    event = next(e for e in events if e.entity_id == current_pick["draft_pick_id"])
    assert event.actor_type == "coach"
    assert event.actor_id == first_pick_coach.coach_id

    # A different coach cannot submit a pick naming entry 0's turn -- even
    # though it is currently nobody else's turn to submit at all (entry 1's
    # turn hasn't arrived), the ownership check on `season_entry_id` itself
    # must reject before any turn/availability logic runs.
    other_session = _login(client, email="preseason-coach-1-4003@example.com")
    other_cookies = {"bbbffl_session": other_session}
    next_board = client.get(f"{coach_api}/board", cookies=own_cookies).json()
    next_pick = next_board["current_pick"]
    cross_team_attempt = client.post(
        f"{coach_api}/pick",
        json={
            "season_entry_id": first_pick_entry.season_entry_id,
            "season_player_id": chosen["season_player_id"],
            "draft_pick_id": next_pick["draft_pick_id"],
        },
        cookies=other_cookies,
    )
    assert cross_team_attempt.status_code == 404
    assert cross_team_attempt.json()["detail"] == "Private resource not found"


def test_shortlist_stays_private_and_operator_controls_remain_admin_only(client):
    database = client.app.state.database
    season, entries, players = _seed_draft_ready_season(database, year=4004, label="Preseason Coach Shortlist")
    owner_entry = entries[0]
    other_entry = entries[1]
    _give_credentials(client.app, owner_entry.season_entry_id)
    _give_credentials(client.app, other_entry.season_entry_id)

    owner_session = _login(client, email="preseason-coach-0-4004@example.com")
    owner_cookies = {"bbbffl_session": owner_session}
    other_session = _login(client, email="preseason-coach-1-4004@example.com")
    other_cookies = {"bbbffl_session": other_session}

    players_resp = client.get(
        f"/api/account/preseason-draft/{season.season_id}/players",
        cookies=owner_cookies,
        params={"availability": "available"},
    ).json()
    target_player = players_resp[0]

    add = client.post(
        f"/api/shortlist/{owner_entry.season_entry_id}/add",
        json={"season_player_id": target_player["season_player_id"]},
        cookies=owner_cookies,
    )
    assert add.status_code == 200, add.text

    shortlist_page = client.get(f"/shortlist/{owner_entry.season_entry_id}", cookies=owner_cookies)
    assert shortlist_page.status_code == 200, shortlist_page.text
    assert f'href="/account/preseason-draft/{season.season_id}"' in shortlist_page.text
    assert "← Back to your pre-season draft" in shortlist_page.text

    # Another coach may not read this private shortlist -- enumeration-safe
    # 404, matching the existing shortlist authorization convention.
    foreign_read = client.get(f"/api/shortlist/{owner_entry.season_entry_id}", cookies=other_cookies)
    assert foreign_read.status_code == 404
    foreign_page = client.get(f"/shortlist/{owner_entry.season_entry_id}", cookies=other_cookies)
    assert foreign_page.status_code == 404

    # Operator-only controls (pause/resume/finalize/reopen/correct) remain
    # reachable on the admin surface -- an explicitly empty session cookie
    # (matching test_midseason_draft_board_api's pattern) resolves to no
    # session, which defaults to admin in dev/test mode -- and remain
    # unreachable via the coach API prefix.
    no_session = {"bbbffl_session": ""}
    admin_api = f"/api/admin/draft/{season.season_id}"
    assert client.post(f"{admin_api}/pause", json={}, cookies=no_session).status_code == 200
    assert client.post(f"{admin_api}/resume", json={}, cookies=no_session).status_code == 200
    admin_page = client.get(f"/admin/draft/{season.season_id}", cookies=no_session)
    assert admin_page.status_code == 200
    assert "Pause / resume" in admin_page.text
    assert "Finalise draft" in admin_page.text
    assert "Danger zone" in admin_page.text

    coach_api = f"/api/account/preseason-draft/{season.season_id}"
    for path in ("pause", "resume", "finalize", "reopen", "correct"):
        response = client.post(f"{coach_api}/{path}", json={}, cookies=owner_cookies)
        assert response.status_code == 404, f"{path} unexpectedly reachable on the coach router: {response.status_code}"


def test_polling_and_focus_refresh_wiring_present_on_coach_preseason_page(client):
    database = client.app.state.database
    season, entries, players = _seed_draft_ready_season(database, year=4005, label="Preseason Coach Polling")
    entry = entries[0]
    _give_credentials(client.app, entry.season_entry_id)
    session = _login(client, email="preseason-coach-0-4005@example.com")
    cookies = {"bbbffl_session": session}

    page = client.get(f"/account/preseason-draft/{season.season_id}", cookies=cookies)
    assert page.status_code == 200, page.text
    assert "const AUTO_REFRESH_MS = 12000" in page.text
    assert "document.addEventListener('visibilitychange', refreshAfterReturning)" in page.text
    assert "window.addEventListener('focus', refreshAfterReturning)" in page.text
    assert "renderPlayers(document.getElementById('player-search').value)" in page.text
