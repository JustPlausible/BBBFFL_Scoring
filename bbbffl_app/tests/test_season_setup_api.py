"""Issue #237's Season setup driven through the real HTTP routes and a real
Scorer browser session: a clean, production-like database is taken from no
season at all to a browser-operable preseason draft (and, on a completed
home-and-away season, through Finals and SuperScore initialization)
without SQL, UUID entry or replay tooling. Also covers authorization,
CSRF, season-scoped grants, refusals (409, state unchanged) and afl-api
evidence failures (503)."""

import json
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.afl_client import AflApiConnectionError
from app.audit import ActorContext
from app.authorization import Role
from tests.finals_seeding_helpers import build_2026_replay_season
from tests.season_setup_helpers import STRUCTURE_TABLES, SetupAfl, table_counts

PASSWORD = "correct horse battery staple"  # noqa: S105 -- test fixture password, not a real credential


@pytest.fixture
def setup_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    from app.main import app

    with TestClient(app) as client:
        client.app.state.afl_client = SetupAfl()
        yield client
    db_path.unlink(missing_ok=True)


def _extract(pattern, html):
    match = re.search(pattern, html)
    assert match, pattern
    return match.group(1)


def _create_season_with_entries(client, year=2027, entry_count=10):
    """The existing Season Centre browser API (ambient operator in this
    test's open dev mode): a season, ten coaches and ten teams."""
    season_id = client.post("/api/admin/season-centre/seasons", json={"year": year, "label": f"{year} BBBFFL"}).json()[
        "season_id"
    ]
    for number in range(1, entry_count + 1):
        coach = client.post("/api/admin/season-centre/coaches", json={"display_name": f"Coach {number}"}).json()
        response = client.post(
            f"/api/admin/season-centre/{season_id}/entries",
            json={"coach_id": coach["coach_id"], "team_name": f"Team {number}"},
        )
        assert response.status_code == 200, response.text
    return season_id


def _scorer_session(client, *, email="scorer@example.com", season_id=None):
    """Log in as a coach identity holding a Scorer grant and activate it,
    exactly as the account page does; returns (headers, coach_id)."""
    state = client.app.state
    coach = state.identities.create_coach("Season Scorer", email=email)
    state.credentials.set_password(coach.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    state.role_grants.grant(
        coach.coach_id, Role.SCORER.value, season_id=season_id, actor=ActorContext.anonymous_operator("admin")
    )
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
    account = client.get("/account")
    switch = client.post(
        "/api/context/role",
        json={"role": "scorer"},
        headers={"X-CSRF-Token": _extract(r'name="csrf_token" value="([^"]+)"', account.text)},
    )
    assert switch.status_code == 200, switch.text
    return coach.coach_id


def _setup_csrf(client, season_id):
    page = client.get(f"/admin/season-setup/{season_id}")
    assert page.status_code == 200
    assert "Season setup" in page.text
    return {"X-CSRF-Token": json.loads(_extract(r"const csrf=(\"[^\"]+\")", page.text))}


def test_clean_database_to_browser_operable_preseason_draft_without_sql_uuids_or_replay(setup_client):
    client = setup_client
    season_id = _create_season_with_entries(client)
    centre = client.get(f"/api/admin/season-centre/{season_id}").json()
    assert centre["links"]["season_setup"] == f"/admin/season-setup/{season_id}"

    scorer_id = _scorer_session(client)
    headers = _setup_csrf(client, season_id)
    setup = client.get(f"/api/admin/season-setup/{season_id}").json()
    assert setup["next_step"] == "player_pool"

    afl_seasons = client.get(f"/api/admin/season-setup/{season_id}/afl-seasons").json()["seasons"]
    [chosen] = [s for s in afl_seasons if s["matches_season_year"]]

    pool = client.post(
        f"/api/admin/season-setup/{season_id}/player-pool",
        json={"afl_season_id": chosen["afl_season_id"], "reason": "Populate 2027 pool"},
        headers=headers,
    )
    assert pool.status_code == 200, pool.text
    assert pool.json()["result"]["inserted"] == 60
    assert pool.json()["setup"]["next_step"] == "ordinary_competition"

    ordinary = client.post(
        f"/api/admin/season-setup/{season_id}/ordinary-competition", json={"reason": "2027 structure"}, headers=headers
    )
    assert ordinary.status_code == 200, ordinary.text
    assert ordinary.json()["result"]["round_count"] == 20

    preview = client.get(
        f"/api/admin/season-setup/{season_id}/opening-round", params={"afl_season_id": chosen["afl_season_id"]}
    ).json()
    assert preview["applicable"] and preview["ready"]
    opening = client.post(
        f"/api/admin/season-setup/{season_id}/opening-round",
        json={
            "afl_season_id": chosen["afl_season_id"],
            "targets": [
                {"afl_club_id": rule["afl_club_id"], "bbbffl_round_number": rule["recommended_bbbffl_round_number"]}
                for rule in preview["rules"]
            ],
            "reason": "2027 Opening Round byes",
        },
        headers=headers,
    )
    assert opening.status_code == 200, opening.text

    squad = client.post(
        f"/api/admin/season-setup/{season_id}/squad-limit", json={"squad_limit": 4, "reason": "2027"}, headers=headers
    )
    assert squad.status_code == 200, squad.text
    entries = next(s for s in squad.json()["setup"]["steps"] if s["key"] == "entries")["facts"]["entries"]
    order = [entry["season_entry_id"] for entry in entries]
    draft = client.post(
        f"/api/admin/season-setup/{season_id}/draft-order",
        json={"ordered_entry_ids": order, "reason": "Agreed 2027 draft order"},
        headers=headers,
    )
    assert draft.status_code == 200, draft.text
    step = next(s for s in draft.json()["setup"]["steps"] if s["key"] == "draft_order")
    assert step["status"] == "complete"

    # The existing coach/Scorer draft surfaces now operate on it.
    board = client.get(f"/api/admin/draft/{season_id}/board")
    assert board.status_code == 200, board.text
    assert board.json()["readiness"]["ready"] is True
    assert board.json()["current_pick"]["current_season_entry_id"] == order[0]

    # ...and the first-pick Coach can make the opening selection from their
    # own draft page, with no further setup.
    coach_client = TestClient(client.app)
    first_coach_id = next(e["coach_id"] for e in entries if e["season_entry_id"] == order[0])
    assert (
        coach_client.post(
            f"/api/admin/season-centre/coaches/{first_coach_id}", json={"email": "first-pick@example.com"}
        ).status_code
        == 200
    )
    client.app.state.credentials.set_password(first_coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    login_page = coach_client.get("/login")
    coach_client.post(
        "/login",
        data={
            "email": "first-pick@example.com",
            "password": PASSWORD,
            "csrf_token": _extract(r'name="csrf_token" value="([^"]+)"', login_page.text),
        },
        follow_redirects=False,
    )
    assert coach_client.get(f"/account/preseason-draft/{season_id}").status_code == 200
    player = coach_client.get(f"/api/account/preseason-draft/{season_id}/players").json()
    first_available = next(item for item in player if item["availability"] == "available")
    pick = coach_client.post(
        f"/api/account/preseason-draft/{season_id}/pick",
        json={"season_entry_id": order[0], "season_player_id": first_available["season_player_id"]},
    )
    assert pick.status_code == 200, pick.text
    assert pick.json()["status"]["completed_picks"] == 1
    assert client.get(f"/admin/draft/{season_id}").status_code == 200
    operator = TestClient(client.app)  # the Secretary/Admin Season Centre view
    assert operator.get(f"/api/admin/season-centre/{season_id}").json()["links"]["draft"] == f"/admin/draft/{season_id}"

    # Every setup mutation is attributed to the signed-in Scorer.
    rows = client.app.state.database.execute(
        "SELECT action, actor_id, actor_role FROM audit_event WHERE action IN "
        "('player_pool.season.refreshed','season.ordinary_competition.initialized','opening_round.rule.accepted',"
        "'ownership.squad_limit.configured','draft.order.accepted')"
    ).fetchall()
    assert {row["action"] for row in rows} == {
        "player_pool.season.refreshed",
        "season.ordinary_competition.initialized",
        "opening_round.rule.accepted",
        "ownership.squad_limit.configured",
        "draft.order.accepted",
    }
    assert {(row["actor_id"], row["actor_role"]) for row in rows} == {(scorer_id, "scorer")}

    # Repeating a completed step is an explicit no-op, not a duplicate.
    before = table_counts(client.app.state.database, *STRUCTURE_TABLES)
    again = client.post(
        f"/api/admin/season-setup/{season_id}/ordinary-competition", json={"reason": "retry"}, headers=headers
    )
    assert again.status_code == 200 and again.json()["result"]["created"] is False
    assert table_counts(client.app.state.database, *STRUCTURE_TABLES) == before


def test_refusals_are_409_with_a_diagnosis_and_change_nothing(setup_client):
    client = setup_client
    season_id = _create_season_with_entries(client, entry_count=3)
    before = table_counts(client.app.state.database, *STRUCTURE_TABLES)
    response = client.post(
        f"/api/admin/season-setup/{season_id}/draft-order", json={"ordered_entry_ids": [], "reason": "too early"}
    )
    assert response.status_code == 409
    assert "exactly 10 season entries" in response.json()["detail"]
    finals = client.post(f"/api/admin/season-setup/{season_id}/finals", json={"reason": "too early"})
    assert finals.status_code == 409
    assert "ordinary competition" in finals.json()["detail"]
    missing_reason = client.post(f"/api/admin/season-setup/{season_id}/ordinary-competition", json={})
    assert missing_reason.status_code == 409
    assert table_counts(client.app.state.database, *STRUCTURE_TABLES) == before
    assert client.get("/api/admin/season-setup/not-a-season").status_code == 404


def test_afl_api_failure_is_reported_as_setup_evidence_unavailable(setup_client):
    client = setup_client
    season_id = _create_season_with_entries(client)

    class Down(SetupAfl):
        def get_season_players(self, afl_season_id):
            raise AflApiConnectionError("/api/v1/seasons/77/players")

    client.app.state.afl_client = Down()
    before = table_counts(client.app.state.database, *STRUCTURE_TABLES)
    response = client.post(
        f"/api/admin/season-setup/{season_id}/player-pool", json={"afl_season_id": 77, "reason": "populate"}
    )
    assert response.status_code == 503
    assert "afl-api could not supply" in response.json()["detail"]
    assert table_counts(client.app.state.database, *STRUCTURE_TABLES) == before


def test_session_writes_need_csrf_and_coaches_or_other_season_scopes_are_refused(setup_client):
    client = setup_client
    old_season = _create_season_with_entries(client, year=2026)
    season_id = _create_season_with_entries(client, year=2027)
    _scorer_session(client, email="scoped@example.com", season_id=old_season)

    # A Scorer grant scoped to 2026 does not reach 2027 setup at all.
    assert client.get(f"/api/admin/season-setup/{season_id}").status_code == 403
    assert client.get(f"/api/admin/season-setup/{old_season}").status_code == 200
    # Cookie-authenticated writes without the double-submit token are refused.
    refused = client.post(f"/api/admin/season-setup/{old_season}/ordinary-competition", json={"reason": "x"})
    assert refused.status_code == 403
    headers = _setup_csrf(client, old_season)
    accepted = client.post(
        f"/api/admin/season-setup/{old_season}/ordinary-competition", json={"reason": "x"}, headers=headers
    )
    assert accepted.status_code == 200, accepted.text

    # A signed-in Coach (no delegated role) has no setup authority.
    coach_client = TestClient(client.app)
    coach_id = client.app.state.identities.list_entries(old_season)[0].coach_id
    assert (
        coach_client.post(
            f"/api/admin/season-centre/coaches/{coach_id}", json={"email": "plain-coach@example.com"}
        ).status_code
        == 200
    )
    client.app.state.credentials.set_password(coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    login_page = coach_client.get("/login")
    coach_client.post(
        "/login",
        data={
            "email": "plain-coach@example.com",
            "password": PASSWORD,
            "csrf_token": _extract(r'name="csrf_token" value="([^"]+)"', login_page.text),
        },
        follow_redirects=False,
    )
    assert coach_client.get(f"/api/admin/season-setup/{old_season}").status_code == 403
    assert coach_client.post(f"/api/admin/season-setup/{old_season}/finals", json={"reason": "x"}).status_code == 403


def test_finals_then_superscore_through_the_browser_api_once_the_home_and_away_season_is_final(setup_client):
    client = setup_client
    database = client.app.state.database
    built = build_2026_replay_season(database=database, year=2031)
    season_id = built["season"].season_id

    superscore_first = client.post(f"/api/admin/season-setup/{season_id}/superscore", json={"reason": "early"})
    assert superscore_first.status_code == 409
    assert "initialize Finals first" in superscore_first.json()["detail"]

    finals = client.post(f"/api/admin/season-setup/{season_id}/finals", json={"reason": "Finals from the ladder"})
    assert finals.status_code == 200, finals.text
    assert finals.json()["result"]["seed_source"] == "ladder"
    superscore = client.post(f"/api/admin/season-setup/{season_id}/superscore", json={"reason": "SuperScore"})
    assert superscore.status_code == 200, superscore.text
    steps = {step["key"]: step["status"] for step in superscore.json()["setup"]["steps"]}
    assert steps["finals"] == steps["superscore"] == "complete"

    # The existing preflight index now lists every Finals week by name.
    rounds = client.get("/api/admin/round-preflight").json()["rounds"]
    finals_labels = [r["round_label"] for r in rounds if r["season_id"] == season_id and r["round_type"] == "finals"]
    assert finals_labels == ["Finals Week 1", "Finals Week 2", "Preliminary Final", "Grand Final"]


def test_the_setup_page_and_post_write_view_survive_an_ambiguous_ordinary_structure(setup_client):
    client = setup_client
    season_id = _create_season_with_entries(client)
    seasons = client.app.state.seasons
    rules = seasons.create_rules_version(season_id, "ordinary", 1, "Rules")
    seasons.create_competition(season_id, rules.rules_version_id, "ordinary", "Ordinary A", "ordinary")
    seasons.create_competition(season_id, rules.rules_version_id, "ordinary-b", "Ordinary B", "ordinary")
    view = client.get(f"/api/admin/season-setup/{season_id}")
    assert view.status_code == 200
    assert next(s for s in view.json()["steps"] if s["key"] == "ordinary_competition")["status"] == "conflict"
    squad = client.post(f"/api/admin/season-setup/{season_id}/squad-limit", json={"squad_limit": 4, "reason": "x"})
    assert squad.status_code == 200, squad.text
    assert squad.json()["result"]["changed"] is True
