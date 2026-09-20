"""Issue #181: the shared draft-board experience applied to the mid-season
draft -- coach self-service selection, the audited Scorer/Admin proxy path,
board/player-browser reuse, and human-readable status output.

Builds a mid-season draft through to `draft_open` exactly the way
`tests/test_midseason_draft_api.py`'s end-to-end test does (two delistings
from the worst-placed entry, no trades), then exercises the new
`/board`, `/players`, and relaxed `/pick` authorization surface.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext, AuditEventRepository
from app.season import SeasonRepository
from tests.midseason_draft_helpers import build_season

PASSWORD = "correct horse battery staple"


@pytest.fixture
def midseason_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _login(client, *, email, password=PASSWORD):
    login_page = client.get("/login")
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": match.group(1)},
        cookies=login_page.cookies,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.cookies.get("bbbffl_session")


def _give_credentials(app, season_entry_id, *, email):
    coach = app.state.identities.get_current_coach(season_entry_id)
    app.state.identities.update_coach(coach.coach_id, email=email, actor=ActorContext.anonymous_operator("admin"))
    app.state.credentials.set_password(coach.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    return coach


def _build_open_draft(client, *, year):
    """Reaches `draft_open` with exactly two vacancies for the worst-placed
    entry, mirroring `tests/test_midseason_draft_api.py`'s own end-to-end
    flow, trimmed to skip trades entirely."""
    database = client.app.state.database
    ctx = build_season(database, year=year, trigger_round=10, squad_limit=4, regular_season_round_count=12)
    season, entries = ctx["season"], ctx["entries"]
    SeasonRepository(database).set_midseason_draft_trigger_round(season.season_id, 10)
    api = f"/api/admin/midseason-draft/{season.season_id}"

    confirmed = client.post(f"{api}/confirm-ladder", json={"competition_id": ctx["competition"].competition_id})
    assert confirmed.status_code == 200, confirmed.text
    assert client.post(f"{api}/open-delisting-window", json={}).status_code == 200

    worst = entries[9]
    worst_squad = ctx["ownership"].current_squad(worst.season_entry_id)
    for player in worst_squad[:2]:
        response = client.post(
            f"{api}/delisting",
            json={"season_entry_id": worst.season_entry_id, "season_player_id": player.season_player_id},
        )
        assert response.status_code == 200

    assert client.post(f"{api}/lock-delistings", json={}).status_code == 200
    generated = client.post(f"{api}/generate-selections", json={})
    assert generated.status_code == 200, generated.text
    assert generated.json()["draft"]["state"] == "draft_open"
    return season, entries, ctx


def test_conduct_board_uses_generated_picks_for_participants_and_progress(midseason_client):
    """Codex review, PR #225 (P2): a mid-season draft's `target_squad_size`
    is the season-wide squad *limit* (4 here), not a uniform per-team pick
    count -- mid-season picks are vacancy-based. The worst entry delisted
    exactly two players, so its own progress target must read 2, never the
    squad limit."""
    client = midseason_client
    season, entries, ctx = _build_open_draft(client, year=3006)
    worst = entries[9]
    api = f"/api/admin/midseason-draft/{season.season_id}"

    board = client.get(f"{api}/board").json()
    worst_progress = next(t for t in board["team_progress"] if t["season_entry_id"] == worst.season_entry_id)
    assert worst_progress["target_count"] == 2
    assert worst_progress["drafted_count"] == 0

    # Every other entry has a full squad and no generated pick. The frozen
    # ladder order remains available as context, but marks that team as a
    # non-participant and does not present it as selection progress.
    full_squad_entry = entries[0]
    full_squad_order = next(o for o in board["order"] if o["season_entry_id"] == full_squad_entry.season_entry_id)
    assert full_squad_order["allocated_pick_count"] == 0
    assert full_squad_entry.season_entry_id not in {team["season_entry_id"] for team in board["team_progress"]}

    generated = client.app.state.draft.picks(season.season_id, draft_kind="midseason")
    displayed = [board["current_pick"], *board["upcoming_picks"]]
    assert [pick["draft_pick_id"] for pick in displayed] == [pick.draft_pick_id for pick in generated]
    assert full_squad_entry.season_entry_id not in {pick["current_season_entry_id"] for pick in displayed}

    _give_credentials(client.app, full_squad_entry.season_entry_id, email="full-squad-coach@example.com")
    session = _login(client, email="full-squad-coach@example.com")
    account = client.get("/account", cookies={"bbbffl_session": session})
    assert account.status_code == 200, account.text
    assert f"/account/midseason-draft/{season.season_id}" in account.text
    assert "0 picks — your team is not participating in selections" in account.text

    page = client.get(f"/account/midseason-draft/{season.season_id}", cookies={"bbbffl_session": session})
    assert page.status_code == 200, page.text
    assert "Teams with 0 picks are identified in the frozen draft order above." in page.text
    assert "not participating" in page.text


def test_coach_can_make_their_own_midseason_selection(midseason_client):
    client = midseason_client
    season, entries, ctx = _build_open_draft(client, year=3001)
    worst = entries[9]
    _give_credentials(client.app, worst.season_entry_id, email="worst-coach@example.com")
    session = _login(client, email="worst-coach@example.com")
    cookies = {"bbbffl_session": session}

    admin_api = f"/api/admin/midseason-draft/{season.season_id}"
    coach_api = f"/api/account/midseason-draft/{season.season_id}"
    account = client.get("/account", cookies=cookies)
    assert account.status_code == 200, account.text
    conduct_url = f"/account/midseason-draft/{season.season_id}"
    assert conduct_url in account.text
    assert "2 generated picks" in account.text
    conduct_page = client.get(conduct_url, cookies=cookies)
    assert conduct_page.status_code == 200, conduct_page.text
    team = client.app.state.identities.get_public_team(worst.season_entry_id)
    assert f"BBBFFL {team.team_name} Mid-season Draft" in conduct_page.text
    assert "/api/account/midseason-draft" in conduct_page.text
    assert "Admin token" not in conduct_page.text
    assert "Pre-draft readiness" not in conduct_page.text
    assert "Proxy provenance" not in conduct_page.text
    assert f'href="/admin/midseason-draft/{season.season_id}"' not in conduct_page.text
    assert "A Scorer/Admin manages post-draft trading and completion." in conduct_page.text
    assert "const AUTO_REFRESH_MS = 12000" in conduct_page.text
    assert "await refresh(null, true)" in conduct_page.text
    assert "if (!successMessage && !requireFresh) return refreshInFlight" in conduct_page.text
    assert "queuedRefreshMessage = successMessage" in conduct_page.text
    assert "return refresh(message)" in conduct_page.text
    assert "const requestId = ++latestPlayerRequest" in conduct_page.text
    assert "requestId !== latestPlayerRequest" in conduct_page.text
    assert "liveBoard.current_pick.draft_pick_id !== currentPickId" in conduct_page.text
    assert "document.addEventListener('visibilitychange', refreshAfterReturning)" in conduct_page.text
    assert "window.addEventListener('focus', refreshAfterReturning)" in conduct_page.text
    assert "It’s your turn — your team now owns the current pick." in conduct_page.text
    assert "renderPlayers(document.getElementById('player-search').value)" in conduct_page.text

    shortlist_page = client.get(f"/shortlist/{worst.season_entry_id}", cookies=cookies)
    assert shortlist_page.status_code == 200, shortlist_page.text
    assert f'href="{conduct_url}"' in shortlist_page.text
    assert "← Back to your mid-season draft" in shortlist_page.text

    admin_page = client.get(f"/admin/midseason-draft/{season.season_id}/conduct", cookies=cookies)
    assert admin_page.status_code == 403
    assert client.get(f"{admin_api}/board", cookies=cookies).status_code == 403

    operator_page = client.get(f"/admin/midseason-draft/{season.season_id}/conduct", cookies={"bbbffl_session": ""})
    assert operator_page.status_code == 200, operator_page.text
    assert "Pre-draft readiness" in operator_page.text
    assert "Proxy provenance" in operator_page.text
    assert f'href="/admin/midseason-draft/{season.season_id}"' in operator_page.text

    board = client.get(f"{coach_api}/board", cookies=cookies).json()
    assert board["current_pick"]["current_season_entry_id"] == worst.season_entry_id
    assert board["draft_kind"] == "midseason"

    players = client.get(f"{coach_api}/players", cookies=cookies, params={"availability": "available"}).json()
    assert len(players) == 2
    # Mid-season browsing shows current-season-to-date context, not the
    # preseason draft's previous-season label.
    assert all(item["stats_label"] == "Current season to date" for item in players)
    chosen = players[0]

    response = client.post(
        f"{coach_api}/pick",
        json={
            "season_entry_id": worst.season_entry_id,
            "season_player_id": chosen["season_player_id"],
            "draft_pick_id": board["current_pick"]["draft_pick_id"],
        },
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Codex review, PR #225 (P1): a coach's own pick response must be the
    # shared, participate-safe board -- never `_status`, which exposes
    # every team's delistings/trade proposals/reasons and is otherwise
    # gated behind the full midseason_draft.manage authority.
    assert body["draft_kind"] == "midseason"
    assert "delistings" not in body
    assert "trades" not in body
    assert body["status"]["completed_picks"] == 1

    # Codex review, PR #225 (P2): a coach's own self-service pick must
    # never be labelled a Scorer/Admin proxy entry -- `pick_view` treats
    # only a non-"coach" completion actor_type as a genuine proxy, and the
    # audit event itself must record the coach as the real actor_type
    # (never anonymous_operator, reserved for the proxy surface).
    completed = next(p for p in body["completed_picks"] if p["draft_pick_id"] == board["current_pick"]["draft_pick_id"])
    assert completed["proxy"] is None
    events = AuditEventRepository(client.app.state.database).list_events(action="draft.pick.completed")
    event = next(e for e in events if e.entity_id == board["current_pick"]["draft_pick_id"])
    assert event.actor_type == "coach"
    coach = client.app.state.identities.get_current_coach(worst.season_entry_id)
    assert event.actor_id == coach.coach_id

    squad = ctx["ownership"].current_squad(worst.season_entry_id)
    assert chosen["season_player_id"] in {row.season_player_id for row in squad}


def test_stale_pick_cannot_double_select_or_take_an_already_taken_player(midseason_client):
    """Issue #181's concurrency requirement, exercised through the new
    coach-facing HTTP surface: the authoritative
    `MidseasonDraftRepository.execute_pick` (via `app.draft.DraftRepository.
    execute_pick`) still refuses a second submission for an
    already-completed pick and a player someone else just took -- this
    thin route never re-implements or weakens that check."""
    client = midseason_client
    season, entries, ctx = _build_open_draft(client, year=3005)
    worst = entries[9]
    _give_credentials(client.app, worst.season_entry_id, email="stale-coach@example.com")
    session = _login(client, email="stale-coach@example.com")
    cookies = {"bbbffl_session": session}

    api = f"/api/account/midseason-draft/{season.season_id}"
    board = client.get(f"{api}/board", cookies=cookies).json()
    current_pick = board["current_pick"]
    pool = client.get(f"{api}/players", cookies=cookies, params={"availability": "available"}).json()
    first_choice, second_choice = pool[0], pool[1]

    first = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": worst.season_entry_id,
            "season_player_id": first_choice["season_player_id"],
            "draft_pick_id": current_pick["draft_pick_id"],
        },
        cookies=cookies,
    )
    assert first.status_code == 200, first.text

    # Resubmitting the exact same (now-completed) pick must not double-
    # select -- neither a second acquisition of the same player nor an
    # acquisition of a different one against a slot that no longer exists.
    replay = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": worst.season_entry_id,
            "season_player_id": second_choice["season_player_id"],
            "draft_pick_id": current_pick["draft_pick_id"],
        },
        cookies=cookies,
    )
    assert replay.status_code >= 400

    squad = ctx["ownership"].current_squad(worst.season_entry_id)
    assert second_choice["season_player_id"] not in {row.season_player_id for row in squad}
    assert sum(1 for row in squad if row.season_player_id == first_choice["season_player_id"]) == 1

    # A stale attempt to take the player someone else (here: the same
    # completed pick) already claimed a moment earlier is refused too.
    next_board = client.get(f"{api}/board", cookies=cookies).json()
    stale_take = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": worst.season_entry_id,
            "season_player_id": first_choice["season_player_id"],
            "draft_pick_id": next_board["current_pick"]["draft_pick_id"],
        },
        cookies=cookies,
    )
    assert stale_take.status_code >= 400


def test_coach_cannot_make_a_selection_for_another_teams_pick(midseason_client):
    client = midseason_client
    season, entries, ctx = _build_open_draft(client, year=3002)
    worst = entries[9]
    other_coach_entry = entries[0]
    _give_credentials(client.app, other_coach_entry.season_entry_id, email="other-coach@example.com")
    session = _login(client, email="other-coach@example.com")
    cookies = {"bbbffl_session": session}

    api = f"/api/account/midseason-draft/{season.season_id}"
    coach_page = client.get(f"/account/midseason-draft/{season.season_id}", cookies=cookies)
    assert coach_page.status_code == 200, coach_page.text
    assert "Read-only until it is your team’s turn." in coach_page.text
    assert (
        "const canPick = MY_SEASON_ENTRY_ID == null || "
        "MY_SEASON_ENTRY_ID === board.current_pick.current_season_entry_id" in coach_page.text
    )
    board = client.get(f"{api}/board", cookies=cookies).json()
    current_pick = board["current_pick"]
    assert current_pick["current_season_entry_id"] == worst.season_entry_id

    pool = client.get(f"{api}/players", cookies=cookies, params={"availability": "available"}).json()
    response = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": worst.season_entry_id,
            "season_player_id": pool[0]["season_player_id"],
            "draft_pick_id": current_pick["draft_pick_id"],
        },
        cookies=cookies,
    )
    # A coach acting as themselves must never be able to submit a payload
    # naming a different team's `season_entry_id` -- enumeration-safe 404,
    # not 403 (matches `require_entry_context`'s existing convention).
    assert response.status_code == 404
    assert response.json()["detail"] == "Private resource not found"

    unchanged = client.get(f"{api}/board", cookies=cookies).json()
    assert unchanged["current_pick"]["draft_pick_id"] == current_pick["draft_pick_id"]


def test_scorer_admin_proxy_pick_still_works_without_a_coach_session(midseason_client):
    client = midseason_client
    season, entries, ctx = _build_open_draft(client, year=3003)
    api = f"/api/admin/midseason-draft/{season.season_id}"
    board = client.get(f"{api}/board").json()
    current_pick = board["current_pick"]
    pool = client.get(f"{api}/available-players").json()

    response = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": current_pick["current_season_entry_id"],
            "season_player_id": pool[0]["season_player_id"],
            "draft_pick_id": current_pick["draft_pick_id"],
            "scorer_name": "Scorer Sam",
            "reason": "coach unavailable, proxy entry",
        },
    )
    assert response.status_code == 200, response.text
    completed = next(
        p for p in response.json()["completed_picks"] if p["draft_pick_id"] == current_pick["draft_pick_id"]
    )
    assert completed["proxy"] == {
        "operator_name": "Scorer Sam",
        "operator_role": "admin",
        "reason": "coach unavailable, proxy entry",
        "on_behalf_of_team": current_pick["current_team_name"],
    }


def test_status_response_is_human_readable(midseason_client):
    client = midseason_client
    database = client.app.state.database
    ctx = build_season(database, year=3004, trigger_round=10, squad_limit=4, regular_season_round_count=12)
    season, entries = ctx["season"], ctx["entries"]
    SeasonRepository(database).set_midseason_draft_trigger_round(season.season_id, 10)
    api = f"/api/admin/midseason-draft/{season.season_id}"
    confirmed = client.post(f"{api}/confirm-ladder", json={"competition_id": ctx["competition"].competition_id})
    body = confirmed.json()
    # Every order row names both the raw id (secondary/audit detail, per
    # issue #181) and a human-readable team/coach label.
    first = body["order"][0]
    assert first["season_entry_id"] == entries[9].season_entry_id
    assert first["team_name"]
    assert first["coach_display_name"]
    assert body["ladder_snapshot"]["rows"][0]["team_name"]

    ladder_preview = client.get(f"{api}/ladder-preview", params={"competition_id": ctx["competition"].competition_id})
    # Already confirmed in this test, but the endpoint itself must still
    # resolve human-readable rows the same way pre-confirmation would.
    assert ladder_preview.status_code == 200
    assert ladder_preview.json()["reverse_order_preview"][0]["team_name"]


def test_ladder_preview_rejects_a_competition_id_from_a_different_season(midseason_client):
    """Codex review, PR #225 (P2): `competition_id` is caller-controlled --
    without validating it belongs to the URL's `season_id`, a season-scoped
    Scorer (or an operator who pastes the wrong id) could preview a
    *different* season's ladder entirely."""
    client = midseason_client
    database = client.app.state.database
    season_a = build_season(database, year=3010, trigger_round=10, squad_limit=4, regular_season_round_count=12)
    season_b = build_season(database, year=3011, trigger_round=10, squad_limit=4, regular_season_round_count=12)
    SeasonRepository(database).set_midseason_draft_trigger_round(season_a["season"].season_id, 10)

    response = client.get(
        f"/api/admin/midseason-draft/{season_a['season'].season_id}/ladder-preview",
        params={"competition_id": season_b["competition"].competition_id},
    )
    assert response.status_code == 400
    assert "ordinary competition belonging to this season" in response.json()["detail"]


def test_trade_legs_are_human_readable(midseason_client):
    """The mid-season operations page (issue #181) renders proposed trades
    with team/player names, not raw ids -- `_status`'s trade legs must
    carry `from_team`/`to_team`/`player` labels."""
    client = midseason_client
    database = client.app.state.database
    ctx = build_season(database, year=3012, trigger_round=10, squad_limit=4, regular_season_round_count=12)
    season, entries = ctx["season"], ctx["entries"]
    SeasonRepository(database).set_midseason_draft_trigger_round(season.season_id, 10)
    api = f"/api/admin/midseason-draft/{season.season_id}"
    client.post(f"{api}/confirm-ladder", json={"competition_id": ctx["competition"].competition_id})
    client.post(f"{api}/open-delisting-window", json={})

    from_entry, to_entry = entries[0], entries[1]
    player = ctx["ownership"].current_squad(from_entry.season_entry_id)[0]
    trade = client.post(
        f"{api}/trade",
        json={
            "legs": [
                {
                    "leg_type": "player",
                    "from_season_entry_id": from_entry.season_entry_id,
                    "to_season_entry_id": to_entry.season_entry_id,
                    "season_player_id": player.season_player_id,
                }
            ]
        },
    )
    assert trade.status_code == 200, trade.text

    status = client.get(f"{api}/status").json()
    assert len(status["trades"]) == 1
    leg = status["trades"][0]["legs"][0]
    assert leg["from_team"]["team_name"]
    assert leg["to_team"]["team_name"]
    assert leg["player"]["display_name"]
