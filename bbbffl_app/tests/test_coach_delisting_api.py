"""Coach self-service mid-season delisting (issue #226) -- exercised
through the real HTTP surface (`app.routes.coach_delisting`), the same way
`tests/test_midseason_draft_board_api.py` exercises the shared draft board's
coach self-service pick.

Builds a mid-season draft through to `delisting_open` the same way
`tests/test_midseason_draft_api.py`'s end-to-end test does (issue #226's own
practical regression target: "2026 replay -> Round 10 final -> ladder
confirmed -> Delisting window open -> no delistings yet"), then exercises
the Coach Account discoverability cue, own-squad viewing, submit/withdraw,
cross-team authorization, post-lock refusal, and confirms the existing
Scorer/Admin proxy path is untouched.
"""

import re
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


def _build_delisting_open(client, *, year):
    """Reaches `delisting_open` with no delistings yet -- issue #226's own
    "practical replay regression target"."""
    database = client.app.state.database
    ctx = build_season(database, year=year, trigger_round=10, squad_limit=4, regular_season_round_count=12)
    season, entries = ctx["season"], ctx["entries"]
    SeasonRepository(database).set_midseason_draft_trigger_round(season.season_id, 10)
    api = f"/api/admin/midseason-draft/{season.season_id}"
    confirmed = client.post(f"{api}/confirm-ladder", json={"competition_id": ctx["competition"].competition_id})
    assert confirmed.status_code == 200, confirmed.text
    opened = client.post(f"{api}/open-delisting-window", json={})
    assert opened.status_code == 200, opened.text
    return season, entries, ctx


def test_account_page_shows_delisting_cue_only_while_the_window_is_open(midseason_client):
    client = midseason_client
    season, entries, ctx = _build_delisting_open(client, year=4001)
    worst = entries[9]
    _give_credentials(client.app, worst.season_entry_id, email="worst-coach@example.com")
    session = _login(client, email="worst-coach@example.com")
    cookies = {"bbbffl_session": session}

    account = client.get("/account", cookies=cookies)
    assert account.status_code == 200
    assert "Mid-season delisting is open" in account.text
    assert "/account/delisting" in account.text
    assert "/conduct" not in account.text

    # A coach with no team in any season currently `delisting_open` sees no
    # cue and no implication that delistings can currently be changed.
    other_ctx = build_season(client.app.state.database, year=4002, trigger_round=10, regular_season_round_count=12)
    other_entry = other_ctx["entries"][0]
    _give_credentials(client.app, other_entry.season_entry_id, email="other-season-coach@example.com")
    other_session = _login(client, email="other-season-coach@example.com")
    other_account = client.get("/account", cookies={"bbbffl_session": other_session})
    assert other_account.status_code == 200
    assert "Mid-season delisting is open" not in other_account.text


def test_coach_can_view_own_squad_and_delisting_state(midseason_client):
    client = midseason_client
    season, entries, ctx = _build_delisting_open(client, year=4003)
    worst = entries[9]
    _give_credentials(client.app, worst.season_entry_id, email="worst-coach@example.com")
    session = _login(client, email="worst-coach@example.com")
    cookies = {"bbbffl_session": session}

    page = client.get("/account/delisting", cookies=cookies)
    assert page.status_code == 200
    team = client.app.state.identities.get_public_team(worst.season_entry_id)
    assert team.team_name in page.text

    status = client.get(f"/api/account/delisting/{worst.season_entry_id}/status", cookies=cookies)
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["is_open"] is True
    assert body["draft_state"] == "delisting_open"
    squad = ctx["ownership"].current_squad(worst.season_entry_id)
    assert {p["season_player_id"] for p in body["squad"]} == {row.season_player_id for row in squad}
    assert all(p["delisted"] is False for p in body["squad"])
    assert body["delistings"] == []


def test_coach_squad_is_ordered_by_full_player_name(midseason_client):
    client = midseason_client
    season, entries, ctx = _build_delisting_open(client, year=4010)
    entry = entries[9]
    squad = ctx["ownership"].current_squad(entry.season_entry_id)
    names = ["Amy Young", "Madonna", "Zoe Adams", "Ben Brown"]
    for ownership, display_name in zip(squad, names, strict=True):
        player = ctx["player_pool"].get_by_id(ownership.season_player_id)
        ctx["player_pool"].refresh_player(season.season_id, player.canonical_player_id, display_name)

    _give_credentials(client.app, entry.season_entry_id, email="alphabetical-coach@example.com")
    session = _login(client, email="alphabetical-coach@example.com")
    status = client.get(
        f"/api/account/delisting/{entry.season_entry_id}/status",
        cookies={"bbbffl_session": session},
    )

    assert status.status_code == 200, status.text
    assert [player["display_name"] for player in status.json()["squad"]] == sorted(names, key=str.casefold)


def test_rendered_coach_page_uses_status_route_for_initial_load(midseason_client):
    """The browser's automatic read uses the API's `/status` route while
    submit and withdraw keep their existing mutation URLs."""
    client = midseason_client
    _, entries, _ = _build_delisting_open(client, year=4009)
    entry = entries[9]
    _give_credentials(client.app, entry.season_entry_id, email="browser-contract-coach@example.com")
    session = _login(client, email="browser-contract-coach@example.com")

    page = client.get("/account/delisting", cookies={"bbbffl_session": session})
    assert page.status_code == 200
    script_match = re.search(r"<script>([\s\S]*)</script>", page.text)
    assert script_match, "inline script not found in the rendered Coach delisting page"
    script = script_match.group(1)

    initial_read = re.search(r"async function refresh\(.*?await api\(([^\n]+)\);", script, re.DOTALL)
    assert initial_read, "refresh() initial API request not found in the rendered Coach delisting page"
    assert initial_read.group(1) == "`${API}/status`"
    assert "await api(`${API}/submit`," in script
    assert "await api(`${API}/${delistingId}/withdraw`," in script


def test_coach_can_submit_then_withdraw_their_own_delisting(midseason_client):
    client = midseason_client
    season, entries, ctx = _build_delisting_open(client, year=4004)
    worst = entries[9]
    _give_credentials(client.app, worst.season_entry_id, email="worst-coach@example.com")
    session = _login(client, email="worst-coach@example.com")
    cookies = {"bbbffl_session": session}
    api = f"/api/account/delisting/{worst.season_entry_id}"

    squad = ctx["ownership"].current_squad(worst.season_entry_id)
    target = squad[0].season_player_id

    submitted = client.post(
        f"{api}/submit", json={"season_player_id": target, "reason": "swap needed"}, cookies=cookies
    )
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert len(body["delistings"]) == 1
    delisting = body["delistings"][0]
    assert delisting["season_player_id"] == target
    assert delisting["season_entry_id"] == worst.season_entry_id
    assert next(p for p in body["squad"] if p["season_player_id"] == target)["delisted"] is True

    # Audit provenance: a genuine coach self-service action, not the
    # anonymous_operator proxy actor type.
    events = AuditEventRepository(client.app.state.database).list_events(action="midseason.delisting.submitted")
    event = next(e for e in events if e.entity_id == delisting["delisting_id"])
    assert event.actor_type == "coach"
    coach = client.app.state.identities.get_current_coach(worst.season_entry_id)
    assert event.actor_id == coach.coach_id

    withdrawn = client.post(f"{api}/{delisting['delisting_id']}/withdraw", json={"reason": None}, cookies=cookies)
    assert withdrawn.status_code == 200, withdrawn.text
    body = withdrawn.json()
    assert body["delistings"] == []
    assert next(p for p in body["squad"] if p["season_player_id"] == target)["delisted"] is False

    # Change of mind: withdrawing then resubmitting is supported by the
    # underlying repository semantics, not a new "draft state" layer.
    resubmitted = client.post(f"{api}/submit", json={"season_player_id": target}, cookies=cookies)
    assert resubmitted.status_code == 200, resubmitted.text
    assert len(resubmitted.json()["delistings"]) == 1


def test_coach_cannot_submit_a_delisting_for_another_team(midseason_client):
    client = midseason_client
    season, entries, ctx = _build_delisting_open(client, year=4005)
    worst, other = entries[9], entries[0]
    _give_credentials(client.app, other.season_entry_id, email="other-coach@example.com")
    session = _login(client, email="other-coach@example.com")
    cookies = {"bbbffl_session": session}

    worst_squad = ctx["ownership"].current_squad(worst.season_entry_id)
    response = client.post(
        f"/api/account/delisting/{worst.season_entry_id}/submit",
        json={"season_player_id": worst_squad[0].season_player_id},
        cookies=cookies,
    )
    # Enumeration-safe 404, matching `require_entry_context`'s existing
    # convention (the same one the mid-season pick endpoint uses).
    assert response.status_code == 404
    assert response.json()["detail"] == "Private resource not found"
    assert ctx["ownership"].current_squad(worst.season_entry_id) == worst_squad


def test_coach_cannot_withdraw_another_teams_delisting(midseason_client):
    client = midseason_client
    season, entries, ctx = _build_delisting_open(client, year=4006)
    worst, other = entries[9], entries[0]
    _give_credentials(client.app, worst.season_entry_id, email="worst-coach@example.com")
    worst_session = _login(client, email="worst-coach@example.com")
    worst_squad = ctx["ownership"].current_squad(worst.season_entry_id)
    submitted = client.post(
        f"/api/account/delisting/{worst.season_entry_id}/submit",
        json={"season_player_id": worst_squad[0].season_player_id},
        cookies={"bbbffl_session": worst_session},
    )
    assert submitted.status_code == 200
    delisting_id = submitted.json()["delistings"][0]["delisting_id"]

    _give_credentials(client.app, other.season_entry_id, email="other-coach@example.com")
    other_session = _login(client, email="other-coach@example.com")
    other_cookies = {"bbbffl_session": other_session}

    # Naming the victim's own season_entry_id in the URL still 404s --
    # `require_entry_context` refuses before ownership of the entry itself
    # is ever established for this principal.
    cross_entry = client.post(
        f"/api/account/delisting/{worst.season_entry_id}/{delisting_id}/withdraw",
        json={"reason": None},
        cookies=other_cookies,
    )
    assert cross_entry.status_code == 404

    # Even naming the attacker's *own* entry alongside the victim's
    # delisting id must not withdraw it -- the delisting's own
    # `season_entry_id` is checked against the resolved entry, not just
    # whichever entry happens to appear in the URL.
    other_squad = ctx["ownership"].current_squad(other.season_entry_id)
    if other_squad:
        client.post(
            f"/api/account/delisting/{other.season_entry_id}/submit",
            json={"season_player_id": other_squad[0].season_player_id},
            cookies=other_cookies,
        )
    cross_id = client.post(
        f"/api/account/delisting/{other.season_entry_id}/{delisting_id}/withdraw",
        json={"reason": None},
        cookies=other_cookies,
    )
    assert cross_id.status_code == 404

    still_active = client.app.state.midseason_draft.get_delisting(delisting_id)
    assert still_active.withdrawn_at is None


def test_coach_mutation_refused_once_the_scorer_locks_delistings(midseason_client):
    client = midseason_client
    season, entries, ctx = _build_delisting_open(client, year=4007)
    worst = entries[9]
    _give_credentials(client.app, worst.season_entry_id, email="worst-coach@example.com")
    session = _login(client, email="worst-coach@example.com")
    cookies = {"bbbffl_session": session}
    api = f"/api/account/delisting/{worst.season_entry_id}"

    squad = ctx["ownership"].current_squad(worst.season_entry_id)
    submitted = client.post(f"{api}/submit", json={"season_player_id": squad[0].season_player_id}, cookies=cookies)
    assert submitted.status_code == 200
    delisting_id = submitted.json()["delistings"][0]["delisting_id"]

    # httpx's TestClient merges per-request cookies into the client's own
    # jar, so the coach session above would otherwise leak into this
    # "legacy admin credential" call below and turn it into a 403.
    client.cookies.delete("bbbffl_session")
    locked = client.post(f"/api/admin/midseason-draft/{season.season_id}/lock-delistings", json={})
    assert locked.status_code == 200, locked.text

    blocked_withdraw = client.post(f"{api}/{delisting_id}/withdraw", json={"reason": None}, cookies=cookies)
    assert blocked_withdraw.status_code == 409

    another_target = ctx["ownership"].current_squad(entries[8].season_entry_id)
    _give_credentials(client.app, entries[8].season_entry_id, email="second-coach@example.com")
    second_session = _login(client, email="second-coach@example.com")
    blocked_submit = client.post(
        f"/api/account/delisting/{entries[8].season_entry_id}/submit",
        json={"season_player_id": another_target[0].season_player_id},
        cookies={"bbbffl_session": second_session},
    )
    assert blocked_submit.status_code == 409

    status = client.get(f"{api}/status", cookies=cookies)
    assert status.status_code == 200
    assert status.json()["is_open"] is False
    assert status.json()["draft_state"] == "delistings_locked"


def test_scorer_admin_proxy_delisting_still_works_alongside_coach_self_service(midseason_client):
    """Issue #226: the normal workflow is now coach self-service, but the
    existing Scorer/Admin proxy path (`app.routes.midseason_draft`) must
    remain fully functional and untouched, including for a *different*
    team than the one a coach just handled themselves."""
    client = midseason_client
    season, entries, ctx = _build_delisting_open(client, year=4008)
    worst, other = entries[9], entries[8]
    _give_credentials(client.app, worst.season_entry_id, email="worst-coach@example.com")
    session = _login(client, email="worst-coach@example.com")
    worst_squad = ctx["ownership"].current_squad(worst.season_entry_id)
    coach_submitted = client.post(
        f"/api/account/delisting/{worst.season_entry_id}/submit",
        json={"season_player_id": worst_squad[0].season_player_id},
        cookies={"bbbffl_session": session},
    )
    assert coach_submitted.status_code == 200

    # See the equivalent note in
    # test_coach_mutation_refused_once_the_scorer_locks_delistings: clear
    # the coach session so this Scorer/Admin proxy call uses the legacy
    # admin credential, not the coach's own session cookie.
    client.cookies.delete("bbbffl_session")
    other_squad = ctx["ownership"].current_squad(other.season_entry_id)
    proxy = client.post(
        f"/api/admin/midseason-draft/{season.season_id}/delisting",
        json={
            "season_entry_id": other.season_entry_id,
            "season_player_id": other_squad[0].season_player_id,
            "scorer_name": "Scorer Sam",
            "reason": "coach unavailable, proxy entry",
        },
    )
    assert proxy.status_code == 200, proxy.text
    active = [d for d in proxy.json()["delistings"] if not d["withdrawn_at"]]
    assert len(active) == 2
    events = AuditEventRepository(client.app.state.database).list_events(action="midseason.delisting.submitted")
    # One coach self-service submission and one Scorer/Admin proxy
    # submission -- the proxy path is recorded with its own distinct
    # `anonymous_operator` actor type, never mislabelled as the coach's.
    assert {e.actor_type for e in events} == {"coach", "anonymous_operator"}
