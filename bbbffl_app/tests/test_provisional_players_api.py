"""Issue #242 HTTP/browser-flow coverage: the Coach nomination surface, the
Scorer/Administrator Provisional players page/JSON API, and the persistent
dashboard notices (`app.routes.provisional_players`, plus the Scorer/Coach
dashboard/account-page integrations) driven through real HTTP requests and
real Coach/Scorer/Administrator sessions -- proves the role boundary, CSRF
and season-scoped-grant wiring end-to-end, the same way `tests/
test_ladder_tie_ruling_api.py` proves `app.routes.ladder_tie_ruling`'s.
Domain-level coverage (creation, detection, reconciliation safety) lives in
`tests/test_provisional_players.py`."""

import json
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.authorization import Role

PASSWORD = "correct horse battery staple"  # noqa: S105 -- test fixture password, not a real credential


@pytest.fixture
def provisional_client(monkeypatch):
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


def _seed(client, year, *, entry_count=2, squad_limit=5):
    state = client.app.state
    season = state.seasons.create_season(year, f"{year} BBBFFL")
    coaches = [state.identities.create_coach(f"Coach {i}") for i in range(entry_count)]
    entries = [
        state.identities.create_entry(season.season_id, f"licence-{year}-{i}", coach.coach_id, f"Team {i}")
        for i, coach in enumerate(coaches)
    ]
    from app.player_pool import OwnershipRepository

    OwnershipRepository(state.database).configure_squad_limit(season.season_id, squad_limit)
    return season, coaches, entries


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


def _coach_session(client, coach, *, email):
    state = client.app.state
    state.identities.update_coach(coach.coach_id, email=email, actor=ActorContext.anonymous_operator("admin"))
    state.credentials.set_password(coach.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    _login(client, email)


def _role_session(client, role: Role, *, email, season_id=None):
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


def _management_csrf(client, season_id):
    page = client.get(f"/scorer/provisional-players/{season_id}")
    assert page.status_code == 200
    assert "Provisional players" in page.text
    return {"X-CSRF-Token": json.loads(_extract(r"const csrf=(\"[^\"]+\")", page.text))}


# -- Coach nomination ----------------------------------------------------------


def test_coach_can_nominate_a_missing_player_for_their_own_entry(provisional_client):
    client = provisional_client
    season, coaches, entries = _seed(client, 8101)
    _coach_session(client, coaches[0], email="coach-8101@example.com")

    response = client.post(
        f"/api/account/player-nominations/{entries[0].season_entry_id}",
        json={"season_id": season.season_id, "player_name": "Jordan Newrecruit", "note": "Saw him on club site"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"

    mine = client.get(f"/api/account/player-nominations/{entries[0].season_entry_id}")
    assert mine.status_code == 200
    assert len(mine.json()["nominations"]) == 1


def test_coach_cannot_nominate_for_another_coachs_entry(provisional_client):
    client = provisional_client
    season, coaches, entries = _seed(client, 8102)
    _coach_session(client, coaches[0], email="coach-8102@example.com")

    response = client.post(
        f"/api/account/player-nominations/{entries[1].season_entry_id}",
        json={"season_id": season.season_id, "player_name": "Someone"},
    )
    assert response.status_code == 404  # enumeration-safe, matches require_entry_context convention


def test_coach_cannot_create_a_provisional_player_directly(provisional_client):
    client = provisional_client
    season, coaches, _entries = _seed(client, 8103)
    _coach_session(client, coaches[0], email="coach-8103@example.com")

    response = client.post(
        f"/api/scorer/provisional-players/{season.season_id}/create",
        json={
            "display_name": "Jordan Newrecruit",
            "given_name": "Jordan",
            "family_name": "Newrecruit",
            "note": "verified",
        },
    )
    assert response.status_code == 403, response.text


def test_coach_cannot_reconcile(provisional_client):
    client = provisional_client
    season, coaches, _entries = _seed(client, 8104)
    _coach_session(client, coaches[0], email="coach-8104@example.com")

    response = client.post(
        f"/api/scorer/provisional-players/{season.season_id}/some-player-id/reconcile",
        json={"target_season_player_id": "x", "reason": "y"},
    )
    assert response.status_code == 403, response.text


def test_coach_dashboard_notice_shows_outstanding_provisional_players_on_their_squad(provisional_client):
    client = provisional_client
    season, coaches, entries = _seed(client, 8105)
    from app.player_pool import OwnershipRepository
    from app.provisional_players import ProvisionalPlayerRepository

    player = ProvisionalPlayerRepository(client.app.state.database).create(
        season.season_id,
        display_name="Jordan Newrecruit",
        given_name="Jordan",
        family_name="Newrecruit",
        note="verified",
        actor=ActorContext.anonymous_operator("scorer"),
    )
    OwnershipRepository(client.app.state.database).acquire(
        player.season_player_id, entries[0].season_entry_id, effective_at="2027-01-01"
    )
    _coach_session(client, coaches[0], email="coach-8105@example.com")

    account_page = client.get("/account")
    assert "Jordan Newrecruit" in account_page.text
    assert "awaiting reconciliation" in account_page.text

    notice = client.get(f"/api/account/provisional-notice/{season.season_id}")
    assert notice.status_code == 200
    body = notice.json()
    assert body["outstanding"][0]["on_my_squad"] is True


def test_coach_dashboard_notice_disappears_after_reconciliation(provisional_client):
    client = provisional_client
    season, coaches, entries = _seed(client, 8106)
    from app.player_pool import OwnershipRepository, PlayerPoolRepository
    from app.provisional_players import ProvisionalPlayerRepository

    provisional = ProvisionalPlayerRepository(client.app.state.database)
    player = provisional.create(
        season.season_id,
        display_name="Jordan Newrecruit",
        given_name="Jordan",
        family_name="Newrecruit",
        note="verified",
        actor=ActorContext.anonymous_operator("scorer"),
    )
    OwnershipRepository(client.app.state.database).acquire(
        player.season_player_id, entries[0].season_entry_id, effective_at="2027-01-01"
    )
    canonical = PlayerPoolRepository(client.app.state.database).refresh_player(
        season.season_id, 12345, "Jordan Newrecruit"
    )
    provisional.reconcile(
        season.season_id,
        player.season_player_id,
        canonical.season_player_id,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="confirmed",
    )
    _coach_session(client, coaches[0], email="coach-8106@example.com")

    account_page = client.get("/account")
    assert "awaiting reconciliation" not in account_page.text


# -- Scorer/Administrator management -------------------------------------------


def test_ambient_operator_can_create_list_and_reconcile(provisional_client):
    client = provisional_client
    season, _coaches, _entries = _seed(client, 8107)

    created = client.post(
        f"/api/scorer/provisional-players/{season.season_id}/create",
        json={
            "display_name": "Jordan Newrecruit",
            "given_name": "Jordan",
            "family_name": "Newrecruit",
            "note": "verified via club website",
        },
    )
    assert created.status_code == 200, created.text
    season_player_id = created.json()["outstanding"][0]["season_player_id"]

    from app.player_pool import PlayerPoolRepository

    canonical = PlayerPoolRepository(client.app.state.database).refresh_player(
        season.season_id, 5001, "Jordan Newrecruit"
    )

    reconciled = client.post(
        f"/api/scorer/provisional-players/{season.season_id}/{season_player_id}/reconcile",
        json={"target_season_player_id": canonical.season_player_id, "reason": "confirmed same player"},
    )
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["outstanding"] == []


def test_creation_requires_a_reason_and_400s(provisional_client):
    client = provisional_client
    season, _coaches, _entries = _seed(client, 8108)
    response = client.post(
        f"/api/scorer/provisional-players/{season.season_id}/create",
        json={"display_name": "X", "given_name": "X", "family_name": "Y", "note": "   "},
    )
    assert response.status_code == 400, response.text


def test_scorer_session_requires_csrf_to_reconcile(provisional_client):
    client = provisional_client
    season, _coaches, _entries = _seed(client, 8109)
    _role_session(client, Role.SCORER, email="scorer-8109@example.com", season_id=season.season_id)

    from app.player_pool import PlayerPoolRepository
    from app.provisional_players import ProvisionalPlayerRepository

    player = ProvisionalPlayerRepository(client.app.state.database).create(
        season.season_id,
        display_name="Jordan Newrecruit",
        given_name="Jordan",
        family_name="Newrecruit",
        note="verified",
        actor=ActorContext.anonymous_operator("scorer"),
    )
    canonical = PlayerPoolRepository(client.app.state.database).refresh_player(
        season.season_id, 5002, "Jordan Newrecruit"
    )

    without_csrf = client.post(
        f"/api/scorer/provisional-players/{season.season_id}/{player.season_player_id}/reconcile",
        json={"target_season_player_id": canonical.season_player_id, "reason": "confirmed"},
    )
    assert without_csrf.status_code == 403, without_csrf.text

    headers = _management_csrf(client, season.season_id)
    with_csrf = client.post(
        f"/api/scorer/provisional-players/{season.season_id}/{player.season_player_id}/reconcile",
        json={"target_season_player_id": canonical.season_player_id, "reason": "confirmed"},
        headers=headers,
    )
    assert with_csrf.status_code == 200, with_csrf.text


def test_repeated_reconciliation_is_a_409(provisional_client):
    client = provisional_client
    season, _coaches, _entries = _seed(client, 8110)
    from app.player_pool import PlayerPoolRepository
    from app.provisional_players import ProvisionalPlayerRepository

    provisional = ProvisionalPlayerRepository(client.app.state.database)
    player = provisional.create(
        season.season_id,
        display_name="Jordan Newrecruit",
        given_name="Jordan",
        family_name="Newrecruit",
        note="verified",
        actor=ActorContext.anonymous_operator("scorer"),
    )
    pool = PlayerPoolRepository(client.app.state.database)
    canonical = pool.refresh_player(season.season_id, 5003, "Jordan Newrecruit")
    other = pool.refresh_player(season.season_id, 5004, "Someone Else")
    provisional.reconcile(
        season.season_id,
        player.season_player_id,
        canonical.season_player_id,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="first",
    )

    response = client.post(
        f"/api/scorer/provisional-players/{season.season_id}/{player.season_player_id}/reconcile",
        json={"target_season_player_id": other.season_player_id, "reason": "second attempt"},
    )
    assert response.status_code == 409, response.text


def test_nomination_can_be_used_to_create_a_provisional_player(provisional_client):
    client = provisional_client
    season, coaches, entries = _seed(client, 8111)
    _coach_session(client, coaches[0], email="coach-8111@example.com")
    nomination = client.post(
        f"/api/account/player-nominations/{entries[0].season_entry_id}",
        json={"season_id": season.season_id, "player_name": "Jordan Newrecruit"},
    ).json()
    client.cookies.clear()  # back to the ambient operator default for the Scorer/Admin action below

    response = client.post(
        f"/api/scorer/provisional-players/{season.season_id}/create",
        json={
            "display_name": "Jordan Newrecruit",
            "given_name": "Jordan",
            "family_name": "Newrecruit",
            "note": "confirmed with the club",
            "nomination_id": nomination["nomination_id"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["pending_nominations"] == []


def test_secretary_is_denied_management_access(provisional_client):
    client = provisional_client
    season, _coaches, _entries = _seed(client, 8112)
    _role_session(client, Role.SECRETARY, email="secretary-8112@example.com", season_id=season.season_id)

    response = client.get(f"/api/scorer/provisional-players/{season.season_id}")
    assert response.status_code == 403, response.text


def test_unknown_season_is_404(provisional_client):
    client = provisional_client
    response = client.get("/api/scorer/provisional-players/not-a-real-season")
    assert response.status_code == 404


# -- Scorer dashboard integration -----------------------------------------------


def test_outstanding_provisional_player_appears_in_scorer_dashboard_attention(provisional_client):
    client = provisional_client
    from app.provisional_players import ProvisionalPlayerRepository

    season, _coaches, _entries = _seed(client, 8113)
    # A round must exist for the ordinary dashboard to have anything to show.
    competition = client.app.state.seasons.create_competition(
        season.season_id,
        client.app.state.seasons.create_rules_version(season.season_id, "canonical", 1, "Rules").rules_version_id,
        "ordinary",
        "Home and Away",
        "ordinary",
    )
    client.app.state.seasons.create_round(competition.competition_id, "R1", "Round 1", 1)

    ProvisionalPlayerRepository(client.app.state.database).create(
        season.season_id,
        display_name="Jordan Newrecruit",
        given_name="Jordan",
        family_name="Newrecruit",
        note="verified",
        actor=ActorContext.anonymous_operator("scorer"),
    )

    response = client.get(f"/api/scorer/dashboard?season_id={season.season_id}")
    assert response.status_code == 200, response.text
    codes = {item["code"] for item in response.json()["dashboard"]["attention"]}
    assert "provisional_players:outstanding" in codes


def test_nomination_form_is_offered_during_the_midseason_draft_too(provisional_client):
    """Codex review on PR #258 (P2): the report-a-missing-player form was
    previously gated on `preseason_selection` alone, so it vanished once
    the preseason draft was finalized -- exactly the state a mid-season
    draft is in. It must also appear from `midseason_selection`."""
    client = provisional_client
    from app.season import SeasonRepository
    from tests.midseason_draft_helpers import build_season

    database = client.app.state.database
    ctx = build_season(database, year=8114, trigger_round=10, squad_limit=4, regular_season_round_count=12)
    season, entries = ctx["season"], ctx["entries"]
    SeasonRepository(database).set_midseason_draft_trigger_round(season.season_id, 10)
    api = f"/api/admin/midseason-draft/{season.season_id}"
    assert (
        client.post(f"{api}/confirm-ladder", json={"competition_id": ctx["competition"].competition_id}).status_code
        == 200
    )
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

    coach = database.execute(
        "SELECT coach_id FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
        (worst.season_entry_id,),
    ).fetchone()
    from types import SimpleNamespace

    _coach_session(client, SimpleNamespace(coach_id=coach["coach_id"]), email="coach-8114@example.com")

    account_page = client.get("/account")
    assert account_page.status_code == 200
    assert "Report missing player" in account_page.text
    assert "Missing a player from the draft pool" in account_page.text
