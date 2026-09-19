"""Issue #181: the private coach draft shortlist/planning list.

Covers the domain repository (`app.shortlist.ShortlistRepository`) directly
plus the HTTP surface's privacy boundary -- one coach must never be able to
read or mutate another coach's shortlist, and it must never itself reserve
a player or influence ownership.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.identity import IdentityRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.season import SeasonRepository
from app.shortlist import ShortlistError, ShortlistRepository
from tests.db_helpers import migrated_connection

PASSWORD = "correct horse battery staple"


def _seed_season(database, *, year):
    season = SeasonRepository(database).create_season(year, str(year))
    identities = IdentityRepository(database)
    entry_a = identities.create_entry(
        season.season_id, "licence-a", identities.create_coach(f"Coach A {year}").coach_id, "Team A"
    )
    entry_b = identities.create_entry(
        season.season_id, "licence-b", identities.create_coach(f"Coach B {year}").coach_id, "Team B"
    )
    pool = PlayerPoolRepository(database)
    players = [pool.refresh_player(season.season_id, 5000 + i, f"Shortlist Player {i}") for i in range(5)]
    return season, entry_a, entry_b, players


def test_add_reorder_remove_round_trip():
    database = migrated_connection()
    season, entry_a, _entry_b, players = _seed_season(database, year=4001)
    shortlist = ShortlistRepository(database)
    actor = ActorContext.anonymous_operator("coach")

    shortlist.add_player(entry_a.season_entry_id, players[0].season_player_id, actor=actor)
    shortlist.add_player(entry_a.season_entry_id, players[1].season_player_id, actor=actor)
    shortlist.add_player(entry_a.season_entry_id, players[2].season_player_id, actor=actor)
    items = shortlist.list_items(entry_a.season_entry_id)
    assert [item.season_player_id for item in items] == [p.season_player_id for p in players[:3]]
    assert [item.rank for item in items] == [1, 2, 3]

    reordered = shortlist.reorder(
        entry_a.season_entry_id,
        [players[2].season_player_id, players[0].season_player_id, players[1].season_player_id],
        actor=actor,
    )
    assert [item.season_player_id for item in reordered] == [
        players[2].season_player_id,
        players[0].season_player_id,
        players[1].season_player_id,
    ]
    assert [item.rank for item in reordered] == [1, 2, 3]

    remaining = shortlist.remove_player(entry_a.season_entry_id, players[0].season_player_id, actor=actor)
    assert [item.season_player_id for item in remaining] == [players[2].season_player_id, players[1].season_player_id]
    assert [item.rank for item in remaining] == [1, 2]


def test_cannot_add_the_same_player_twice_or_a_foreign_season_player():
    database = migrated_connection()
    season, entry_a, _entry_b, players = _seed_season(database, year=4002)
    other_season, other_entry, _b2, other_players = _seed_season(database, year=4003)
    shortlist = ShortlistRepository(database)
    actor = ActorContext.anonymous_operator("coach")

    shortlist.add_player(entry_a.season_entry_id, players[0].season_player_id, actor=actor)
    with pytest.raises(ShortlistError):
        shortlist.add_player(entry_a.season_entry_id, players[0].season_player_id, actor=actor)
    with pytest.raises(ShortlistError):
        shortlist.add_player(entry_a.season_entry_id, other_players[0].season_player_id, actor=actor)


def test_reorder_refuses_a_partial_or_foreign_list():
    database = migrated_connection()
    season, entry_a, _entry_b, players = _seed_season(database, year=4004)
    shortlist = ShortlistRepository(database)
    actor = ActorContext.anonymous_operator("coach")
    shortlist.add_player(entry_a.season_entry_id, players[0].season_player_id, actor=actor)
    shortlist.add_player(entry_a.season_entry_id, players[1].season_player_id, actor=actor)

    with pytest.raises(ShortlistError):
        shortlist.reorder(entry_a.season_entry_id, [players[0].season_player_id], actor=actor)
    with pytest.raises(ShortlistError):
        shortlist.reorder(
            entry_a.season_entry_id, [players[0].season_player_id, players[2].season_player_id], actor=actor
        )


def test_shortlist_never_reserves_a_player_or_changes_ownership():
    database = migrated_connection()
    season, entry_a, entry_b, players = _seed_season(database, year=4005)
    OwnershipRepository(database).configure_squad_limit(season.season_id, 5)
    shortlist = ShortlistRepository(database)
    actor = ActorContext.anonymous_operator("coach")
    shortlist.add_player(entry_a.season_entry_id, players[0].season_player_id, actor=actor)

    # Being shortlisted by A does not stop B from acquiring the same player
    # through the ordinary ownership ledger -- the shortlist has no
    # authority over availability at all.
    OwnershipRepository(database).acquire(
        players[0].season_player_id, entry_b.season_entry_id, actor=ActorContext.anonymous_operator("admin")
    )
    owner = OwnershipRepository(database).owner_at(players[0].season_player_id, "9999-12-31")
    assert owner.season_entry_id == entry_b.season_entry_id


def test_suggestion_skips_unavailable_players_and_recomputes_live():
    database = migrated_connection()
    season, entry_a, entry_b, players = _seed_season(database, year=4006)
    OwnershipRepository(database).configure_squad_limit(season.season_id, 5)
    shortlist = ShortlistRepository(database)
    actor = ActorContext.anonymous_operator("coach")
    shortlist.add_player(entry_a.season_entry_id, players[0].season_player_id, actor=actor)
    shortlist.add_player(entry_a.season_entry_id, players[1].season_player_id, actor=actor)

    assert shortlist.suggestion(entry_a.season_entry_id).season_player_id == players[0].season_player_id

    # players[0] becomes unavailable (another team acquires it) -- the
    # suggestion must move to the next still-available preference without
    # any change to the shortlist itself.
    OwnershipRepository(database).acquire(
        players[0].season_player_id, entry_b.season_entry_id, actor=ActorContext.anonymous_operator("admin")
    )
    suggestion = shortlist.suggestion(entry_a.season_entry_id)
    assert suggestion.season_player_id == players[1].season_player_id
    assert [item.season_player_id for item in shortlist.list_items(entry_a.season_entry_id)] == [
        players[0].season_player_id,
        players[1].season_player_id,
    ]


# -- HTTP privacy boundary ---------------------------------------------------


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


def _login(client, *, email, password=PASSWORD):
    import re

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


def _seed_http_season(app, *, year):
    database = app.state.database
    season, entry_a, entry_b, players = _seed_season(database, year=year)
    coach_a = app.state.identities.get_current_coach(entry_a.season_entry_id)
    coach_b = app.state.identities.get_current_coach(entry_b.season_entry_id)
    app.state.identities.update_coach(
        coach_a.coach_id, email="shortlist-a@example.com", actor=ActorContext.anonymous_operator("admin")
    )
    app.state.identities.update_coach(
        coach_b.coach_id, email="shortlist-b@example.com", actor=ActorContext.anonymous_operator("admin")
    )
    app.state.credentials.set_password(coach_a.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    app.state.credentials.set_password(coach_b.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    return season, entry_a, entry_b, players


def test_coach_can_manage_their_own_shortlist_via_http(client):
    season, entry_a, entry_b, players = _seed_http_season(client.app, year=4101)
    session = _login(client, email="shortlist-a@example.com")
    cookies = {"bbbffl_session": session}

    added = client.post(
        f"/api/shortlist/{entry_a.season_entry_id}/add",
        json={"season_player_id": players[0].season_player_id},
        cookies=cookies,
    )
    assert added.status_code == 200, added.text
    assert len(added.json()["items"]) == 1
    assert added.json()["items"][0]["display_name"] == players[0].display_name


def test_coach_cannot_read_or_mutate_another_coachs_shortlist(client):
    season, entry_a, entry_b, players = _seed_http_season(client.app, year=4102)
    client.app.state.shortlist.add_player(
        entry_b.season_entry_id, players[0].season_player_id, actor=ActorContext.anonymous_operator("coach")
    )
    session = _login(client, email="shortlist-a@example.com")
    cookies = {"bbbffl_session": session}

    read = client.get(f"/api/shortlist/{entry_b.season_entry_id}", cookies=cookies)
    assert read.status_code == 404
    assert read.json()["detail"] == "Private resource not found"

    mutate = client.post(
        f"/api/shortlist/{entry_b.season_entry_id}/add",
        json={"season_player_id": players[1].season_player_id},
        cookies=cookies,
    )
    assert mutate.status_code == 404

    # B's shortlist is untouched by A's attempted mutation.
    unchanged = client.app.state.shortlist.list_items(entry_b.season_entry_id)
    assert len(unchanged) == 1


def test_unauthenticated_request_cannot_read_any_shortlist(client):
    season, entry_a, entry_b, players = _seed_http_season(client.app, year=4103)
    response = client.get(f"/api/shortlist/{entry_a.season_entry_id}")
    assert response.status_code in (401, 403, 404)
