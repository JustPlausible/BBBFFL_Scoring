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
        Match(match_id=100, home_team=CATS, away_team=PIES, status="FINAL")
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
    assert ir["match_state"] in ("yet_to_play", "live", "completed", "unnamed")
    # No AFL stats supplied by the test fixture -> neutral, not invented.
    assert ir["potential_scores"] is None


def test_finalized_result_survives_afl_api_outage(client):
    from app.afl_client import AflApiError
    from app.main import app as fastapi_app

    fastapi_app.state.fake_afl_client.matches = [
        Match(match_id=100, home_team=CATS, away_team=PIES, status="FINAL")
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
