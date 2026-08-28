"""The preseason trade/finalisation window driven entirely through the real
HTTP admin API (roadmap package 15, issue #54) -- proves the whole workflow
(open, trade, status, close, opening squad, rejected post-close mutation,
authorised correction) works end-to-end through app.main.app, not merely
that the underlying repository does.

Uses its own isolated SQLite database and season, exactly like
tests/test_scorer_draft_workflow.py, so it cannot contaminate any other
season's state.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.draft import DraftRepository
from app.identity import IdentityRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.season import SeasonRepository

ENTRIES = 3
SQUAD_LIMIT = 2


@pytest.fixture
def preseason_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _seed_finalized_draft(database):
    season = SeasonRepository(database).create_season(2097, "Preseason API season")
    identities = IdentityRepository(database)
    entries = [
        identities.create_entry(
            season.season_id, f"preseason-api-{n}", identities.create_coach(f"Coach {n}").coach_id, f"Team {n}"
        )
        for n in range(ENTRIES)
    ]
    OwnershipRepository(database).configure_squad_limit(season.season_id, SQUAD_LIMIT)
    pool = PlayerPoolRepository(database)
    players = [pool.refresh_player(season.season_id, n + 1, f"Player {n}") for n in range(ENTRIES * SQUAD_LIMIT + 1)]
    draft = DraftRepository(database)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    for _ in range(ENTRIES * SQUAD_LIMIT):
        pick = draft.next_pick(season.season_id)
        draft.execute_pick(
            season.season_id, pick.current_season_entry_id, players[pick.overall_number - 1].season_player_id
        )
    draft.finalize(season.season_id)
    return season, entries, players


def test_preseason_window_workflow_end_to_end_via_the_admin_api(preseason_client):
    client = preseason_client
    database = client.app.state.database
    season, entries, players = _seed_finalized_draft(database)
    ownership = OwnershipRepository(database)
    api = f"/api/admin/preseason/{season.season_id}"

    # Opening before a window exists reports an empty, unopened status.
    status = client.get(f"{api}/status").json()
    assert status["window"] is None

    opened = client.post(f"{api}/open", json={"reason": "start trading"})
    assert opened.status_code == 200
    assert opened.json()["window"]["closed_at"] is None

    # A second open is a clear conflict, not a silent no-op.
    assert client.post(f"{api}/open", json={}).status_code == 409

    e0, e1 = entries[0].season_entry_id, entries[1].season_entry_id
    squad_e0 = [p.season_player_id for p in ownership.squad_at(e0, "9999-01-01")]
    squad_e1 = [p.season_player_id for p in ownership.squad_at(e1, "9999-01-01")]

    trade_response = client.post(
        f"{api}/trade",
        json={
            "legs": [
                {"season_player_id": squad_e0[0], "from_season_entry_id": e0, "to_season_entry_id": e1},
                {"season_player_id": squad_e1[0], "from_season_entry_id": e1, "to_season_entry_id": e0},
            ],
            "scorer_name": "Steve the Scorer",
            "reason": "agreed trade",
        },
    )
    assert trade_response.status_code == 200
    body = trade_response.json()
    assert len(body["legs"]) == 2

    # An invalid trade (non-owner) reports 409 with structured issues.
    invalid = client.post(
        f"{api}/trade",
        json={
            "legs": [
                {
                    "season_player_id": squad_e0[0],
                    "from_season_entry_id": entries[2].season_entry_id,
                    "to_season_entry_id": e1,
                }
            ]
        },
    )
    assert invalid.status_code == 409
    assert invalid.json()["issues"]

    assert len(client.get(f"{api}/trades").json()) == 1

    close = client.post(f"{api}/close", json={"reason": "opening squads locked"})
    assert close.status_code == 200
    assert close.json()["window"]["closed_at"] is not None

    # Closing again is a clear, distinct-status conflict (locked), not a
    # silent no-op or a generic 409.
    assert client.post(f"{api}/close", json={}).status_code == 423

    # Trades are rejected post-close, at the HTTP boundary too.
    blocked = client.post(
        f"{api}/trade",
        json={"legs": [{"season_player_id": squad_e0[0], "from_season_entry_id": e1, "to_season_entry_id": e0}]},
    )
    assert blocked.status_code == 423

    opening_squad_e0 = client.get(f"{api}/opening-squad", params={"season_entry_id": e0}).json()
    assert len(opening_squad_e0) == SQUAD_LIMIT

    # Authorised correction: swap the frozen wrong_player out for the free agent.
    wrong_player = opening_squad_e0[0]["season_player_id"]
    free_agent = players[-1].season_player_id
    correction = client.post(
        f"{api}/correct-opening-squad",
        json={
            "season_entry_id": e0,
            "remove_season_player_id": wrong_player,
            "add_season_player_id": free_agent,
            "reason": "data entry error found post-closure",
            "scorer_name": "Admin",
        },
    )
    assert correction.status_code == 200
    corrected_squad = client.get(f"{api}/opening-squad", params={"season_entry_id": e0}).json()
    assert free_agent in {row["season_player_id"] for row in corrected_squad}
    assert wrong_player not in {row["season_player_id"] for row in corrected_squad}

    # A correction without a reason is rejected.
    missing_reason = client.post(
        f"{api}/correct-opening-squad",
        json={
            "season_entry_id": e0,
            "remove_season_player_id": free_agent,
            "add_season_player_id": wrong_player,
            "reason": "",
        },
    )
    assert missing_reason.status_code == 400


def test_opening_the_window_before_the_draft_is_finalized_is_rejected(preseason_client):
    client = preseason_client
    database = client.app.state.database
    season = SeasonRepository(database).create_season(2096, "Not yet finalized")
    identities = IdentityRepository(database)
    entries = [
        identities.create_entry(
            season.season_id, f"nf-{n}", identities.create_coach(f"Coach nf {n}").coach_id, f"Team nf {n}"
        )
        for n in range(2)
    ]
    OwnershipRepository(database).configure_squad_limit(season.season_id, 1)
    DraftRepository(database).accept_order(season.season_id, [entry.season_entry_id for entry in entries])

    response = client.post(f"/api/admin/preseason/{season.season_id}/open", json={})
    assert response.status_code == 409


def test_closing_with_invalid_squads_reports_diagnostics_and_does_not_close(preseason_client):
    client = preseason_client
    database = client.app.state.database
    season, entries, _players = _seed_finalized_draft(database)
    ownership = OwnershipRepository(database)
    api = f"/api/admin/preseason/{season.season_id}"
    client.post(f"{api}/open", json={})

    broken_entry = entries[0].season_entry_id
    broken_player = ownership.squad_at(broken_entry, "9999-01-01")[0].season_player_id
    ownership.release(broken_player)

    response = client.post(f"{api}/close", json={})
    assert response.status_code == 409
    issues = response.json()["issues"]
    assert any(issue["season_entry_id"] == broken_entry for issue in issues)

    status = client.get(f"{api}/status").json()
    assert status["window"]["closed_at"] is None
