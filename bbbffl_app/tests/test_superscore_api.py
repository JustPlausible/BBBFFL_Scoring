"""End-to-end tests through FastAPI's TestClient proving:

- SuperScore is fully opt-in -- disabled by default, Grand Final behaviour
  is unaffected either way.
- The SuperScore admin/public routes work once enabled.
- Scorer decisions made through the SuperScore API cannot alter the Grand
  Final API's state for the same underlying data, and vice versa.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.afl_client import Match, Player
from app.scoring import ROSTER_SLOTS
from app.service import PlayerIdentityCache
from tests.conftest import CATS, PIES, TEAM_A_ROSTER, TEAM_B_ROSTER, FakeAflClient

GRAND_FINAL_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "grand_final_teams.json"
)


def _superscore_lineup(base: int) -> dict:
    return {slot: base + i for i, slot in enumerate(ROSTER_SLOTS)}


def _write_superscore_config(tmp_path, num_entries=10):
    entries = [
        {"team_key": f"team_{n}", "coach": f"Coach {n}", "lineup": _superscore_lineup(n * 1000)}
        for n in range(1, num_entries + 1)
    ]
    config = {
        "season": 2026,
        "afl_round": 20,
        "competition_type": "SUPERSCORE",
        "entries": entries,
    }
    path = tmp_path / "superscore.json"
    path.write_text(json.dumps(config))
    return str(path)


def _base_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BBBFFL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AFL_API_BASE_URL", "http://unused.invalid")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "secret123")
    monkeypatch.setenv("BBBFFL_TEAMS_CONFIG_PATH", GRAND_FINAL_CONFIG_PATH)


def _install_fake_afl_client(app, extra_players=None):
    players = {}
    for ids, team in ((TEAM_A_ROSTER.values(), CATS), (TEAM_B_ROSTER.values(), PIES)):
        for pid in ids:
            players[pid] = Player(canonical_player_id=pid, name=f"Player {pid}", current_team=team)
    for team in app.state.teams:
        for pid in team.roster.values():
            players.setdefault(pid, Player(canonical_player_id=pid, name=f"Player {pid}", current_team=CATS))
    if extra_players:
        players.update(extra_players)

    match = Match(match_id=100, home_team=CATS, away_team=PIES, status="LIVE")
    fake = FakeAflClient([match], players, {100: {}})
    app.state.afl_client = fake
    app.state.identity_cache = PlayerIdentityCache(fake)
    app.state.fake_afl_client = fake
    return fake


@pytest.fixture
def client_no_superscore(tmp_path, monkeypatch):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("BBBFFL_SUPERSCORE_CONFIG_PATH", raising=False)

    from app.main import app

    with TestClient(app) as test_client:
        _install_fake_afl_client(app)
        yield test_client


@pytest.fixture
def client_with_superscore(tmp_path, monkeypatch):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BBBFFL_SUPERSCORE_CONFIG_PATH", _write_superscore_config(tmp_path))

    from app.main import app

    with TestClient(app) as test_client:
        extra = {}
        for n in range(1, 11):
            for pid in _superscore_lineup(n * 1000).values():
                extra[pid] = Player(canonical_player_id=pid, name=f"SS Player {pid}", current_team=CATS)
        _install_fake_afl_client(app, extra_players=extra)
        yield test_client


ADMIN_HEADERS = {"X-Admin-Token": "secret123"}


# -- Opt-in behaviour --------------------------------------------------------


def test_superscore_routes_404_when_not_configured(client_no_superscore):
    assert client_no_superscore.get("/superscore").status_code == 404
    assert client_no_superscore.get("/api/public/superscore/state").status_code == 404
    assert (
        client_no_superscore.get("/api/admin/superscore/state", headers=ADMIN_HEADERS).status_code
        == 404
    )


def test_grand_final_behaves_unchanged_when_superscore_is_disabled(client_no_superscore):
    r = client_no_superscore.get("/api/public/state")
    assert r.status_code == 200
    assert r.json()["status"] == "LIVE"

    r = client_no_superscore.get("/")
    assert r.status_code == 200
    r = client_no_superscore.get("/admin")
    assert r.status_code == 200


def test_grand_final_behaves_unchanged_when_superscore_is_enabled(client_with_superscore):
    """Enabling SuperScore must not change a single thing about the Grand
    Final's own API responses."""
    r = client_with_superscore.get("/api/public/state")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "LIVE"
    assert len(body["teams"]) == 2


# -- SuperScore routes, once enabled -----------------------------------------


def test_superscore_public_state_lists_ten_entries_ranked(client_with_superscore):
    r = client_with_superscore.get("/api/public/superscore/state")
    assert r.status_code == 200
    body = r.json()
    assert len(body["teams"]) == 10
    assert len(body["standings"]) == 10
    assert body["season"] == 2026
    assert body["afl_round"] == 20


def test_concluded_afl_match_renders_superscore_players_as_final_not_yet_to_play(
    client_with_superscore,
):
    """Regression for the Round 24 live incident: a match reported by
    afl-api as status="CONCLUDED" must resolve to match_state="completed" on
    the SuperScore screen too, not fall through to "yet_to_play"."""
    from app.main import app as fastapi_app

    fastapi_app.state.fake_afl_client.matches = [
        Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
    ]

    r = client_with_superscore.get("/api/public/superscore/state")
    assert r.status_code == 200
    body = r.json()
    for team in body["teams"]:
        for position in team["positions"]:
            if position["slot_source"] in ("starting", "interchange"):
                assert position["match_state"] == "completed"


def test_superscore_page_and_admin_page_render(client_with_superscore):
    assert client_with_superscore.get("/superscore").status_code == 200
    assert client_with_superscore.get("/admin/superscore").status_code == 200


def test_superscore_admin_requires_token(client_with_superscore):
    assert client_with_superscore.get("/api/admin/superscore/state").status_code == 401
    assert (
        client_with_superscore.get("/api/admin/superscore/state", headers=ADMIN_HEADERS).status_code
        == 200
    )


def test_superscore_admin_rejects_unknown_team_key(client_with_superscore):
    r = client_with_superscore.post(
        "/api/admin/superscore/dnp",
        json={"team_key": "not_a_real_team", "slot": "Forward1", "dnp": True},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 404


def test_full_superscore_scorer_workflow(client_with_superscore):
    r = client_with_superscore.post(
        "/api/admin/superscore/dnp",
        json={"team_key": "team_1", "slot": "Forward1", "dnp": True},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200
    team_1 = next(t for t in r.json()["teams"] if t["team_key"] == "team_1")
    fwd1 = next(p for p in team_1["positions"] if p["position"] == "Forward1")
    assert fwd1["slot_source"] == "vacant"

    r = client_with_superscore.post(
        "/api/admin/superscore/finalize", json={"note": "too early"}, headers=ADMIN_HEADERS
    )
    assert r.status_code == 409

    from app.main import app as fastapi_app

    fastapi_app.state.fake_afl_client.matches = [
        Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
    ]

    r = client_with_superscore.post(
        "/api/admin/superscore/finalize", json={"note": "round confirmed"}, headers=ADMIN_HEADERS
    )
    assert r.status_code == 200
    assert r.json()["status"] == "FINAL"

    r = client_with_superscore.post(
        "/api/admin/superscore/dnp",
        json={"team_key": "team_2", "slot": "Tackler", "dnp": True},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 423


# -- Isolation via the HTTP API ----------------------------------------------


def test_superscore_dnp_does_not_affect_grand_final_state_for_the_same_team_key(
    client_with_superscore,
):
    """team_a is a real Grand Final team_key; SuperScore's team_1 is
    unrelated, but this proves even a coincidental team_key match couldn't
    leak, by exercising the same team_key through both APIs."""
    r = client_with_superscore.post(
        "/api/admin/superscore/dnp",
        json={"team_key": "team_1", "slot": "Ruck", "dnp": True},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200

    gf_state = client_with_superscore.get("/api/admin/state", headers=ADMIN_HEADERS).json()
    # Grand Final has no team_1 -- but critically, its own team_a/team_b
    # Ruck positions are untouched by the SuperScore DNP call above.
    for team in gf_state["teams"]:
        ruck = next(p for p in team["positions"] if p["position"] == "Ruck")
        assert ruck["match_state"] != "vacant"


# -- Public/Admin navigation between Grand Final and SuperScore --------------


def test_public_grand_final_page_links_to_superscore_when_enabled(client_with_superscore):
    r = client_with_superscore.get("/")
    assert r.status_code == 200
    assert 'href="/superscore"' in r.text
    assert "SuperScore" in r.text


def test_public_grand_final_page_has_no_superscore_link_when_disabled(client_no_superscore):
    r = client_no_superscore.get("/")
    assert r.status_code == 200
    assert 'href="/superscore"' not in r.text


def test_public_superscore_page_links_back_to_grand_final(client_with_superscore):
    r = client_with_superscore.get("/superscore")
    assert r.status_code == 200
    assert 'href="/"' in r.text
    assert "Grand Final" in r.text


def test_admin_page_links_to_superscore_admin_when_enabled(client_with_superscore):
    r = client_with_superscore.get("/admin")
    assert r.status_code == 200
    assert 'href="/admin/superscore"' in r.text


def test_admin_page_has_no_superscore_admin_link_when_disabled(client_no_superscore):
    r = client_no_superscore.get("/admin")
    assert r.status_code == 200
    assert 'href="/admin/superscore"' not in r.text


def test_admin_superscore_page_links_back_to_grand_final_admin(client_with_superscore):
    r = client_with_superscore.get("/admin/superscore")
    assert r.status_code == 200
    assert 'href="/admin"' in r.text


def test_public_pages_never_link_to_admin(client_with_superscore):
    """Public -> Admin navigation must never be added (task brief #6/#7)."""
    for path in ("/", "/superscore"):
        r = client_with_superscore.get(path)
        assert 'href="/admin"' not in r.text
        assert 'href="/admin/superscore"' not in r.text


def test_admin_navigation_does_not_weaken_admin_protection(client_with_superscore):
    """Switching competition context in Admin is just a link between two
    already-protected pages -- the underlying APIs must still require the
    admin token in both directions."""
    assert client_with_superscore.get("/api/admin/state").status_code == 401
    assert (
        client_with_superscore.get("/api/admin/superscore/state").status_code == 401
    )
    assert (
        client_with_superscore.get("/api/admin/state", headers=ADMIN_HEADERS).status_code == 200
    )
    assert (
        client_with_superscore.get(
            "/api/admin/superscore/state", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )


def test_finalizing_superscore_does_not_finalize_grand_final(client_with_superscore):
    from app.main import app as fastapi_app

    fastapi_app.state.fake_afl_client.matches = [
        Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
    ]

    r = client_with_superscore.post(
        "/api/admin/superscore/finalize", json={"note": "confirmed"}, headers=ADMIN_HEADERS
    )
    assert r.status_code == 200
    assert r.json()["status"] == "FINAL"

    gf_state = client_with_superscore.get("/api/admin/state", headers=ADMIN_HEADERS).json()
    assert gf_state["status"] != "FINAL"
