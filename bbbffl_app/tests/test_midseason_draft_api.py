"""The mid-season draft workflow driven entirely through the real HTTP admin
API (issue #164) -- proves confirm-ladder, delisting, trade, lock, generate
and pick all work end-to-end through app.main.app, not merely that the
underlying repository does. Mirrors tests/test_preseason_api.py's isolated
per-test SQLite database convention."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import AuditEventRepository
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


def test_operations_player_selector_filters_team_before_global_limit(midseason_client):
    client = midseason_client
    ctx = build_season(
        client.app.state.database,
        year=4011,
        trigger_round=10,
        squad_limit=22,
        regular_season_round_count=12,
    )
    season, entry = ctx["season"], ctx["entries"][9]
    expected_ids = {item.season_player_id for item in ctx["ownership"].current_squad(entry.season_entry_id)}

    players = client.get(
        f"/api/admin/midseason-draft/{season.season_id}/players",
        params={"availability": "owned", "owner_season_entry_id": entry.season_entry_id},
    )

    assert players.status_code == 200, players.text
    assert {item["season_player_id"] for item in players.json()} == expected_ids
    assert len(players.json()) == 22

    page = client.get(f"/admin/midseason-draft/{season.season_id}")
    assert page.status_code == 200, page.text
    assert "/players?availability=owned&owner_season_entry_id=${encodeURIComponent(entryId)}" in page.text


def _open_trade_window(client, *, year):
    ctx = build_season(
        client.app.state.database,
        year=year,
        trigger_round=10,
        squad_limit=4,
        regular_season_round_count=12,
    )
    season = ctx["season"]
    SeasonRepository(client.app.state.database).set_midseason_draft_trigger_round(season.season_id, 10)
    api = f"/api/admin/midseason-draft/{season.season_id}"
    confirmed = client.post(f"{api}/confirm-ladder", json={"competition_id": ctx["competition"].competition_id})
    assert confirmed.status_code == 200, confirmed.text
    opened = client.post(f"{api}/open-delisting-window", json={"reason": "agreed trade window"})
    assert opened.status_code == 200, opened.text
    return ctx, api


def test_operations_trade_recorder_uses_human_choices_and_domain_decisions(midseason_client):
    client = midseason_client
    ctx, api = _open_trade_window(client, year=4012)
    season, team_a, team_b = ctx["season"], ctx["entries"][0], ctx["entries"][1]
    squad_a = ctx["ownership"].current_squad(team_a.season_entry_id)
    squad_b = ctx["ownership"].current_squad(team_b.season_entry_id)

    page = client.get(f"/admin/midseason-draft/{season.season_id}")
    assert page.status_code == 200, page.text
    for control_id in (
        "trade-team-a",
        "trade-team-b",
        "trade-type-a",
        "trade-type-b",
        "trade-asset-a",
        "trade-asset-b",
        "propose-trade-btn",
    ):
        assert f'id="{control_id}"' in page.text
    assert "Team A gives / Team B receives" in page.text
    assert "Team B gives / Team A receives" in page.text
    assert "owner_season_entry_id=${encodeURIComponent(teamId)}" in page.text
    assert "draft_round: Number(asset)" in page.text

    status = client.get(f"{api}/status").json()
    assert status["trade_pick_rounds"] == [1, 2, 3, 4]
    expected_team_names = {
        ctx["identities"].get_public_team(entry.season_entry_id).team_name for entry in (team_a, team_b)
    }
    assert {item["team_name"] for item in status["order"]} >= expected_team_names

    owned_a = client.get(
        f"{api}/players",
        params={"availability": "owned", "owner_season_entry_id": team_a.season_entry_id},
    )
    assert owned_a.status_code == 200, owned_a.text
    assert {item["season_player_id"] for item in owned_a.json()} == {item.season_player_id for item in squad_a}

    # Player-for-pick entry exercises the exact leg shapes emitted by the
    # browser recorder. Rejecting it must leave ownership untouched.
    proposed_pick_trade = client.post(
        f"{api}/trade",
        json={
            "legs": [
                {
                    "leg_type": "player",
                    "from_season_entry_id": team_a.season_entry_id,
                    "to_season_entry_id": team_b.season_entry_id,
                    "season_player_id": squad_a[0].season_player_id,
                },
                {
                    "leg_type": "pick",
                    "from_season_entry_id": team_b.season_entry_id,
                    "to_season_entry_id": team_a.season_entry_id,
                    "draft_round": 1,
                },
            ],
            "reason": "coaches agreed player for round one",
        },
    )
    assert proposed_pick_trade.status_code == 200, proposed_pick_trade.text
    rejected_id = proposed_pick_trade.json()["trade"]["trade_id"]
    rejected = client.post(
        f"{api}/trade/{rejected_id}/decide",
        json={"approve": False, "reason": "coaches withdrew agreement"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["trade"]["status"] == "rejected"
    assert (
        ctx["ownership"].owner_at(squad_a[0].season_player_id, "9999-12-31").season_entry_id == team_a.season_entry_id
    )

    pick_swap = client.post(
        f"{api}/trade",
        json={
            "legs": [
                {
                    "leg_type": "pick",
                    "from_season_entry_id": team_a.season_entry_id,
                    "to_season_entry_id": team_b.season_entry_id,
                    "draft_round": 1,
                },
                {
                    "leg_type": "pick",
                    "from_season_entry_id": team_b.season_entry_id,
                    "to_season_entry_id": team_a.season_entry_id,
                    "draft_round": 2,
                },
            ],
            "reason": "agreed round-pick swap",
        },
    )
    assert pick_swap.status_code == 200, pick_swap.text
    pick_swap_id = pick_swap.json()["trade"]["trade_id"]
    assert (
        client.post(
            f"{api}/trade/{pick_swap_id}/decide",
            json={"approve": False, "reason": "recording test only"},
        ).status_code
        == 200
    )

    # A player-for-player trade can then be reviewed and approved through
    # the same route; the existing domain applies both ownership legs.
    proposed_swap = client.post(
        f"{api}/trade",
        json={
            "legs": [
                {
                    "leg_type": "player",
                    "from_season_entry_id": team_a.season_entry_id,
                    "to_season_entry_id": team_b.season_entry_id,
                    "season_player_id": squad_a[1].season_player_id,
                },
                {
                    "leg_type": "player",
                    "from_season_entry_id": team_b.season_entry_id,
                    "to_season_entry_id": team_a.season_entry_id,
                    "season_player_id": squad_b[0].season_player_id,
                },
            ],
            "reason": "recorded from coaches' chat agreement",
        },
    )
    assert proposed_swap.status_code == 200, proposed_swap.text
    approved_id = proposed_swap.json()["trade"]["trade_id"]
    approved = client.post(
        f"{api}/trade/{approved_id}/decide",
        json={"approve": True, "reason": "Scorer verified both sides"},
    )
    assert approved.status_code == 200, approved.text
    assert (
        ctx["ownership"].owner_at(squad_a[1].season_player_id, "9999-12-31").season_entry_id == team_b.season_entry_id
    )
    assert (
        ctx["ownership"].owner_at(squad_b[0].season_player_id, "9999-12-31").season_entry_id == team_a.season_entry_id
    )

    refreshed = client.get(f"{api}/status").json()
    approved_view = next(trade for trade in refreshed["trades"] if trade["trade_id"] == approved_id)
    assert approved_view["proposal_audit"]["actor_role"] == "admin"
    assert approved_view["proposal_audit"]["reason"] == "recorded from coaches' chat agreement"
    assert approved_view["decision_audit"]["actor_role"] == "admin"
    assert approved_view["decision_audit"]["reason"] == "Scorer verified both sides"
    actions = [
        event.action for event in AuditEventRepository(client.app.state.database).list_events(entity_id=approved_id)
    ]
    assert actions == ["midseason.trade.proposed", "midseason.trade.approved"]


def test_trade_recorder_obeys_pre_and_post_draft_lifecycle(midseason_client):
    client = midseason_client
    database = client.app.state.database
    ctx = build_season(database, year=4013, trigger_round=10, squad_limit=4, regular_season_round_count=12)
    season, entries = ctx["season"], ctx["entries"]
    SeasonRepository(database).set_midseason_draft_trigger_round(season.season_id, 10)
    api = f"/api/admin/midseason-draft/{season.season_id}"
    assert (
        client.post(f"{api}/confirm-ladder", json={"competition_id": ctx["competition"].competition_id}).status_code
        == 200
    )

    player_a = ctx["ownership"].current_squad(entries[0].season_entry_id)[0]
    player_b = ctx["ownership"].current_squad(entries[1].season_entry_id)[0]
    legs = [
        {
            "leg_type": "player",
            "from_season_entry_id": entries[0].season_entry_id,
            "to_season_entry_id": entries[1].season_entry_id,
            "season_player_id": player_a.season_player_id,
        },
        {
            "leg_type": "player",
            "from_season_entry_id": entries[1].season_entry_id,
            "to_season_entry_id": entries[0].season_entry_id,
            "season_player_id": player_b.season_player_id,
        },
    ]
    refused = client.post(f"{api}/trade", json={"legs": legs, "reason": "too early"})
    assert refused.status_code == 409

    assert client.post(f"{api}/open-delisting-window", json={}).status_code == 200
    worst = entries[9]
    delisted = ctx["ownership"].current_squad(worst.season_entry_id)[0]
    assert (
        client.post(
            f"{api}/delisting",
            json={"season_entry_id": worst.season_entry_id, "season_player_id": delisted.season_player_id},
        ).status_code
        == 200
    )
    assert client.post(f"{api}/lock-delistings", json={}).status_code == 200
    assert client.post(f"{api}/generate-selections", json={}).status_code == 200
    pick = client.get(f"{api}/picks").json()[0]
    available = client.get(f"{api}/available-players").json()[0]
    selected = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": pick["current_season_entry_id"],
            "season_player_id": available["season_player_id"],
            "draft_pick_id": pick["draft_pick_id"],
        },
    )
    assert selected.status_code == 200, selected.text
    assert client.get(f"{api}/status").json()["draft"]["state"] == "draft_complete"

    post_draft_trade = client.post(f"{api}/trade", json={"legs": legs, "reason": "post-draft agreement"})
    assert post_draft_trade.status_code == 200, post_draft_trade.text
    post_id = post_draft_trade.json()["trade"]["trade_id"]
    assert (
        client.post(
            f"{api}/trade/{post_id}/decide", json={"approve": True, "reason": "post-draft approval"}
        ).status_code
        == 200
    )

    pick_leg_after_lock = client.post(
        f"{api}/trade",
        json={
            "legs": [
                {
                    "leg_type": "pick",
                    "from_season_entry_id": entries[0].season_entry_id,
                    "to_season_entry_id": entries[1].season_entry_id,
                    "draft_round": 1,
                },
                {
                    "leg_type": "player",
                    "from_season_entry_id": entries[1].season_entry_id,
                    "to_season_entry_id": entries[0].season_entry_id,
                    "season_player_id": player_a.season_player_id,
                },
            ]
        },
    )
    assert pick_leg_after_lock.status_code == 409
    assert "only available before delistings lock" in pick_leg_after_lock.text


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


def test_ordinary_competitions_endpoint_resolves_the_unambiguous_case(midseason_client):
    """Issue #226: the normal case -- exactly one ordinary competition --
    resolves without the operator ever supplying a UUID."""
    client = midseason_client
    database = client.app.state.database
    ctx = build_season(database, trigger_round=10)
    season, competition = ctx["season"], ctx["competition"]

    response = client.get(f"/api/admin/midseason-draft/{season.season_id}/ordinary-competitions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == [{"competition_id": competition.competition_id, "label": competition.label}]


def test_ordinary_competitions_endpoint_offers_a_human_readable_selector_when_ambiguous(midseason_client):
    """Issue #226: when a season somehow has more than one legitimate
    ordinary competition, every one is returned with its human-readable
    label so the operator can choose -- never a bare id list."""
    client = midseason_client
    database = client.app.state.database
    ctx = build_season(database, trigger_round=10)
    season, competition = ctx["season"], ctx["competition"]
    from app.season import SeasonRepository

    seasons = SeasonRepository(database)
    rules = seasons.create_rules_version(season.season_id, "ordinary-2", 1, "Rules 2")
    second = seasons.create_competition(
        season.season_id, rules.rules_version_id, "ordinary-2", "Ordinary (revised)", "ordinary"
    )

    response = client.get(f"/api/admin/midseason-draft/{season.season_id}/ordinary-competitions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {item["competition_id"] for item in body} == {competition.competition_id, second.competition_id}
    assert {item["label"] for item in body} == {competition.label, second.label}


def test_ordinary_competitions_endpoint_is_empty_when_none_configured(midseason_client):
    """Issue #226: fail-closed -- a season with no ordinary competition at
    all returns an empty list rather than inventing or guessing one; the
    operations page disables ladder preview entirely in this case."""
    client = midseason_client
    database = client.app.state.database
    from app.season import SeasonRepository

    seasons = SeasonRepository(database)
    season = seasons.create_season(9300, "9300", regular_season_round_count=10)

    response = client.get(f"/api/admin/midseason-draft/{season.season_id}/ordinary-competitions")
    assert response.status_code == 200, response.text
    assert response.json() == []
