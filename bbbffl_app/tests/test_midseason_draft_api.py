"""The mid-season draft workflow driven entirely through the real HTTP admin
API (issue #164) -- proves confirm-ladder, delisting, trade, lock, generate
and pick all work end-to-end through app.main.app, not merely that the
underlying repository does. Mirrors tests/test_preseason_api.py's isolated
per-test SQLite database convention."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.season import SeasonRepository
from tests.midseason_draft_helpers import build_season


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


def test_midseason_draft_workflow_end_to_end_via_the_admin_api(midseason_client):
    client = midseason_client
    database = client.app.state.database
    ctx = build_season(database, trigger_round=10, squad_limit=4, regular_season_round_count=12)
    season, entries, competition = ctx["season"], ctx["entries"], ctx["competition"]
    SeasonRepository(database).set_midseason_draft_trigger_round(season.season_id, 10)

    empty = client.get(f"/api/admin/midseason-draft/{season.season_id}/status")
    assert empty.status_code == 200 and empty.json()["draft"] is None

    confirmed = client.post(
        f"/api/admin/midseason-draft/{season.season_id}/confirm-ladder",
        json={"competition_id": competition.competition_id, "reason": "round 10 final"},
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["draft"]["state"] == "ladder_confirmed"
    assert len(body["order"]) == 10
    assert body["order"][0]["season_entry_id"] == entries[9].season_entry_id

    opened = client.post(
        f"/api/admin/midseason-draft/{season.season_id}/open-delisting-window", json={"reason": "open trading"}
    )
    assert opened.status_code == 200
    assert opened.json()["draft"]["state"] == "delisting_open"

    worst = entries[9]
    worst_squad = ctx["ownership"].current_squad(worst.season_entry_id)
    delisting = client.post(
        f"/api/admin/midseason-draft/{season.season_id}/delisting",
        json={
            "season_entry_id": worst.season_entry_id,
            "season_player_id": worst_squad[0].season_player_id,
            "reason": "cutting",
        },
    )
    assert delisting.status_code == 200
    assert len(delisting.json()["delistings"]) == 1

    delisting2 = client.post(
        f"/api/admin/midseason-draft/{season.season_id}/delisting",
        json={"season_entry_id": worst.season_entry_id, "season_player_id": worst_squad[1].season_player_id},
    )
    assert delisting2.status_code == 200

    best = entries[0]
    best_squad = ctx["ownership"].current_squad(best.season_entry_id)
    second_squad = ctx["ownership"].current_squad(entries[1].season_entry_id)
    trade = client.post(
        f"/api/admin/midseason-draft/{season.season_id}/trade",
        json={
            "legs": [
                {
                    "leg_type": "player",
                    "from_season_entry_id": best.season_entry_id,
                    "to_season_entry_id": entries[1].season_entry_id,
                    "season_player_id": best_squad[0].season_player_id,
                },
                {
                    "leg_type": "player",
                    "from_season_entry_id": entries[1].season_entry_id,
                    "to_season_entry_id": best.season_entry_id,
                    "season_player_id": second_squad[0].season_player_id,
                },
            ],
            "reason": "socially agreed swap",
        },
    )
    assert trade.status_code == 200, trade.text
    trade_id = trade.json()["trade"]["trade_id"]

    # Locking is refused while the trade is pending.
    blocked = client.post(f"/api/admin/midseason-draft/{season.season_id}/lock-delistings", json={})
    assert blocked.status_code == 409
    assert trade_id in blocked.json()["trade_ids"]

    decided = client.post(
        f"/api/admin/midseason-draft/{season.season_id}/trade/{trade_id}/decide",
        json={"approve": True, "reason": "approved"},
    )
    assert decided.status_code == 200
    assert decided.json()["trade"]["status"] == "approved"

    locked = client.post(f"/api/admin/midseason-draft/{season.season_id}/lock-delistings", json={})
    assert locked.status_code == 200
    assert locked.json()["draft"]["state"] == "delistings_locked"

    generated = client.post(f"/api/admin/midseason-draft/{season.season_id}/generate-selections", json={})
    assert generated.status_code == 200, generated.text
    assert generated.json()["draft"]["state"] == "draft_open"

    pool = client.get(f"/api/admin/midseason-draft/{season.season_id}/available-players").json()
    assert len(pool) == 2

    picks = client.get(f"/api/admin/midseason-draft/{season.season_id}/picks").json()
    for pick, player in zip(picks, pool):
        result = client.post(
            f"/api/admin/midseason-draft/{season.season_id}/pick",
            json={
                "season_entry_id": pick["current_season_entry_id"],
                "season_player_id": player["season_player_id"],
                "draft_pick_id": pick["draft_pick_id"],
            },
        )
        assert result.status_code == 200, result.text

    final_status = client.get(f"/api/admin/midseason-draft/{season.season_id}/status").json()
    assert final_status["draft"]["state"] == "draft_complete"

    closed = client.post(
        f"/api/admin/midseason-draft/{season.season_id}/close-post-draft-trading",
        json={"reason": "Round 11 lockout approaching"},
    )
    assert closed.status_code == 200
    assert closed.json()["draft"]["state"] == "complete"


def test_confirm_ladder_before_configured_trigger_round_is_a_409(midseason_client):
    client = midseason_client
    database = client.app.state.database
    ctx = build_season(database, trigger_round=10)
    season, competition = ctx["season"], ctx["competition"]
    response = client.post(
        f"/api/admin/midseason-draft/{season.season_id}/confirm-ladder",
        json={"competition_id": competition.competition_id},
    )
    assert response.status_code == 409


def test_trigger_round_can_be_set_via_the_admin_api_then_confirm_ladder_succeeds(midseason_client):
    client = midseason_client
    database = client.app.state.database
    ctx = build_season(database, trigger_round=10)
    season, competition = ctx["season"], ctx["competition"]

    set_round = client.post(
        f"/api/admin/midseason-draft/{season.season_id}/trigger-round",
        json={"trigger_round": 10, "reason": "season setup"},
    )
    assert set_round.status_code == 200, set_round.text

    confirmed = client.post(
        f"/api/admin/midseason-draft/{season.season_id}/confirm-ladder",
        json={"competition_id": competition.competition_id},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["draft"]["state"] == "ladder_confirmed"
