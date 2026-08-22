"""End-to-end API tests through FastAPI's TestClient, with app.state.afl_client
swapped for a FakeAflClient so no network/real afl-api is required.

These exercise the same sqlite connection across FastAPI's threadpool-backed
request handlers, which is what caught the need for
check_same_thread=False in app/db.py.
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.afl_client import Match, Player
from app.service import PlayerIdentityCache
from tests.conftest import CATS, PIES, TEAM_A_ROSTER, TEAM_B_ROSTER, FakeAflClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BBBFFL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AFL_API_BASE_URL", "http://unused.invalid")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "secret123")
    monkeypatch.setenv(
        "BBBFFL_TEAMS_CONFIG_PATH",
        os.path.join(os.path.dirname(__file__), "..", "data", "grand_final_teams.json"),
    )

    from app.main import app

    with TestClient(app) as test_client:
        players = {}
        for ids, team in ((TEAM_A_ROSTER.values(), CATS), (TEAM_B_ROSTER.values(), PIES)):
            for pid in ids:
                players[pid] = Player(canonical_player_id=pid, name=f"Player {pid}", current_team=team)

        # The sample team config uses different (100xxx) player IDs; register those too.
        for team in app.state.teams:
            for pid in team.roster.values():
                players.setdefault(
                    pid, Player(canonical_player_id=pid, name=f"Player {pid}", current_team=CATS)
                )

        match = Match(match_id=100, home_team=CATS, away_team=PIES, status="LIVE")
        fake = FakeAflClient([match], players, {100: {}})
        app.state.afl_client = fake
        app.state.identity_cache = PlayerIdentityCache(fake)
        app.state.fake_afl_client = fake  # exposed for tests that need to flip match status
        yield test_client


ADMIN_HEADERS = {"X-Admin-Token": "secret123"}


def test_grand_final_still_resolves_afl_apis_current_round_by_default(client):
    """Grand Final never declares its own season/round, so build_matchup_state
    must keep resolving whichever round afl-api currently considers current
    -- the new season_year/round_number override added for SuperScore is
    opt-in and must not change this default path."""
    from app.main import app as fastapi_app

    fake = fastapi_app.state.fake_afl_client
    r = client.get("/api/public/state")
    assert r.status_code == 200
    assert fake.get_round_calls == [(fake._season.season_id, fake._season.current_round_number)]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_public_state_requires_no_token(client):
    r = client.get("/api/public/state")
    assert r.status_code == 200
    assert r.json()["status"] == "LIVE"


def test_admin_state_requires_token(client):
    assert client.get("/api/admin/state").status_code == 401
    assert client.get("/api/admin/state", headers=ADMIN_HEADERS).status_code == 200


def test_full_scorer_workflow(client):
    team_key = client.get("/api/admin/state", headers=ADMIN_HEADERS).json()["teams"][0]["team_key"]

    r = client.post(
        "/api/admin/dnp",
        json={"team_key": team_key, "slot": "Forward1", "dnp": True},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200
    fwd1 = next(p for p in r.json()["teams"][0]["positions"] if p["position"] == "Forward1")
    assert fwd1["slot_source"] == "vacant"
    assert fwd1["recommended_interchange"] is True

    r = client.post(
        "/api/admin/interchange",
        json={"team_key": team_key, "target_position": "Forward1"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200
    fwd1 = next(p for p in r.json()["teams"][0]["positions"] if p["position"] == "Forward1")
    assert fwd1["slot_source"] == "interchange"

    r = client.post(
        "/api/admin/override",
        json={"team_key": team_key, "position": "Ruck", "override_score": 77, "reason": "test"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200
    ruck = next(p for p in r.json()["teams"][0]["positions"] if p["position"] == "Ruck")
    assert ruck["effective_score"] == 77
    assert ruck["override_reason"] == "test"

    r = client.post("/api/admin/finalize", json={"note": "too early"}, headers=ADMIN_HEADERS)
    assert r.status_code == 409

    from app.main import app as fastapi_app

    fastapi_app.state.fake_afl_client.matches = [
        Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
    ]

    r = client.post("/api/admin/finalize", json={"note": "confirmed"}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "FINAL"

    r = client.post(
        "/api/admin/dnp",
        json={"team_key": team_key, "slot": "Tackler", "dnp": True},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 423


def test_public_state_exposes_starting_player_identity_for_a_dnp_position(client):
    """The public page must keep showing the coach's original selection for
    a DNP'd position -- not just erase it -- per the DNP-visibility brief."""
    team_key = client.get("/api/admin/state", headers=ADMIN_HEADERS).json()["teams"][0]["team_key"]
    client.post(
        "/api/admin/dnp",
        json={"team_key": team_key, "slot": "Forward1", "dnp": True},
        headers=ADMIN_HEADERS,
    )

    r = client.get("/api/public/state")
    assert r.status_code == 200
    fwd1 = next(p for p in r.json()["teams"][0]["positions"] if p["position"] == "Forward1")
    assert fwd1["slot_source"] == "vacant"
    assert fwd1["player_name"] is None
    assert fwd1["starting_dnp"] is True
    assert fwd1["starting_player_name"] is not None


def test_public_state_exposes_starting_player_identity_when_interchange_covers_a_dnp_position(client):
    team_key = client.get("/api/admin/state", headers=ADMIN_HEADERS).json()["teams"][0]["team_key"]
    client.post(
        "/api/admin/dnp",
        json={"team_key": team_key, "slot": "Forward1", "dnp": True},
        headers=ADMIN_HEADERS,
    )
    client.post(
        "/api/admin/interchange",
        json={"team_key": team_key, "target_position": "Forward1"},
        headers=ADMIN_HEADERS,
    )

    r = client.get("/api/public/state")
    assert r.status_code == 200
    fwd1 = next(p for p in r.json()["teams"][0]["positions"] if p["position"] == "Forward1")
    assert fwd1["slot_source"] == "interchange"
    assert fwd1["starting_dnp"] is True
    # The original coach-named starter's identity is still recoverable even
    # though the interchange player is now the effective scorer.
    assert fwd1["starting_player_name"] is not None
    assert fwd1["starting_player_name"] != fwd1["player_name"]


def test_public_state_exposes_interchange_presentation_fields(client):
    r = client.get("/api/public/state")
    assert r.status_code == 200
    ir = r.json()["teams"][0]["interchange"]
    assert set(ir.keys()) == {
        "player_name",
        "afl_club",
        "match_state",
        "dnp",
        "target_position",
        "potential_scores",
    }
    assert ir["match_state"] in ("yet_to_play", "live", "postgame", "completed", "unnamed")
    # No AFL stats supplied by the test fixture -> neutral, not invented.
    assert ir["potential_scores"] is None


def test_concluded_afl_match_renders_rostered_players_as_final_not_yet_to_play(client):
    """Regression for the Round 24 live incident: afl-api's real v1 contract
    reports a completed match as status="CONCLUDED" (not "FINAL"). Before the
    normalizer recognised CONCLUDED, rostered players whose AFL team had
    already finished still showed up as "Yet to play" on the public
    Head-to-Head scoreboard."""
    from app.main import app as fastapi_app

    fastapi_app.state.fake_afl_client.matches = [
        Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
    ]

    r = client.get("/api/public/state")
    assert r.status_code == 200
    body = r.json()
    for team in body["teams"]:
        for position in team["positions"]:
            if position["slot_source"] in ("starting", "interchange"):
                assert position["match_state"] == "completed"
        assert team["interchange"]["match_state"] in ("completed", "unnamed")


def test_postgame_afl_match_renders_rostered_players_as_postgame_not_final(client):
    """A POSTGAME match (siren sounded, stats not yet declared final by
    afl-api) must render distinctly from both "Live" and "Final" over the
    public API -- not collapsed into either, and not implying the scorer
    should be prompted to sign off yet."""
    from app.main import app as fastapi_app

    fastapi_app.state.fake_afl_client.matches = [
        Match(match_id=100, home_team=CATS, away_team=PIES, status="POSTGAME")
    ]

    r = client.get("/api/public/state")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "LIVE"
    assert body["counts"]["postgame"] > 0
    assert body["counts"]["completed"] == 0
    for team in body["teams"]:
        for position in team["positions"]:
            if position["slot_source"] in ("starting", "interchange"):
                assert position["match_state"] == "postgame"
        assert team["interchange"]["match_state"] in ("postgame", "unnamed")


def test_public_state_serves_a_pre_upgrade_finalized_snapshot_without_500ing(client):
    """Regression: /api/public/state must keep serving an already-FINAL
    Grand Final finalised before the football-style display fields (see
    app/presentation.py) existed, rather than KeyError-ing on a dict that
    predates them."""
    from app.main import app as fastapi_app

    fastapi_app.state.fake_afl_client.matches = [
        Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
    ]
    r = client.post("/api/admin/finalize", json={"note": "confirmed"}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    legacy_snapshot = r.json()
    display_keys = (
        "display_goals",
        "display_behinds",
        "display_is_actual_afl",
        "display_adjusted_by_override",
        "football_line",
    )
    for team in legacy_snapshot["teams"]:
        for key in display_keys:
            team.pop(key, None)
        for position in team["positions"]:
            for key in display_keys:
                position.pop(key, None)
    fastapi_app.state.decisions.finalize("confirmed (legacy)", legacy_snapshot)

    r = client.get("/api/public/state")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "FINAL"
    for position in body["teams"][0]["positions"]:
        assert "football_line" in position


def test_finalized_result_survives_afl_api_outage(client):
    from app.afl_client import AflApiError
    from app.main import app as fastapi_app

    fastapi_app.state.fake_afl_client.matches = [
        Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
    ]
    r = client.post("/api/admin/finalize", json={"note": "confirmed"}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "FINAL"

    class ExplodingClient:
        def __getattr__(self, name):
            def _boom(*args, **kwargs):
                raise AflApiError(f"afl-api should not be called after finalize (called {name})")

            return _boom

    fastapi_app.state.afl_client = ExplodingClient()

    r = client.get("/api/public/state")
    assert r.status_code == 200
    assert r.json()["status"] == "FINAL"

    r = client.get("/api/admin/state", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "FINAL"
