"""Issue #190: HTTP surface for the finals preflight/open-round adapter
(`app/routes/finals_preflight.py`) -- exercised end-to-end through the real
FastAPI app, mirroring tests/test_round_preflight.py's `preflight_client`
fixture shape for the ordinary equivalent."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.finals import FinalsBracketRepository
from app.season import SeasonRepository
from tests.finals_helpers import accept_week_mapping, seed_official_result
from tests.finals_seeding_helpers import build_2026_replay_season


@pytest.fixture
def finals_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _seed_bracket(client, year=2026):
    built = build_2026_replay_season(database=client.app.state.database, year=year)
    seasons = SeasonRepository(client.app.state.database)
    rules_row = client.app.state.database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (built["season"].season_id,)
    ).fetchone()
    finals_competition = seasons.create_competition(
        built["season"].season_id, rules_row["rules_version_id"], "finals", "Finals", "finals"
    )
    repo = FinalsBracketRepository(client.app.state.database)
    result = repo.create_bracket(
        built["season"].season_id,
        finals_competition.competition_id,
        built["competition"].competition_id,
        actor=ActorContext.anonymous_operator("setup"),
        reason="route test bracket setup",
    )
    bracket = result["bracket"]
    for week in (1, 2, 3, 4):
        round_id = repo.get_week_round_id(bracket.bracket_id, week)
        accept_week_mapping(client.app.state.database, round_id, year=year, afl_round_id=8000 + week)
    return built, bracket


def test_view_bracket_returns_full_read_model(finals_client):
    _built, bracket = _seed_bracket(finals_client)
    response = finals_client.get(f"/api/admin/finals/{bracket.bracket_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["bracket"]["bracket_id"] == bracket.bracket_id
    assert len(body["seed"]) == 10
    assert len(body["weeks"]) == 4


def test_view_bracket_404s_for_unknown_bracket(finals_client):
    response = finals_client.get("/api/admin/finals/does-not-exist")
    assert response.status_code == 404


def test_week_preflight_reports_blockers_before_mapping_accepted(finals_client):
    built = build_2026_replay_season(database=finals_client.app.state.database, year=2601)
    seasons = SeasonRepository(finals_client.app.state.database)
    rules_row = finals_client.app.state.database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (built["season"].season_id,)
    ).fetchone()
    finals_competition = seasons.create_competition(
        built["season"].season_id, rules_row["rules_version_id"], "finals", "Finals", "finals"
    )
    repo = FinalsBracketRepository(finals_client.app.state.database)
    bracket = repo.create_bracket(
        built["season"].season_id,
        finals_competition.competition_id,
        built["competition"].competition_id,
        actor=ActorContext.anonymous_operator("setup"),
        reason="no mapping yet",
    )["bracket"]

    response = finals_client.get(f"/api/admin/finals/{bracket.bracket_id}/weeks/1")
    assert response.status_code == 200
    body = response.json()
    assert not body["readiness"]["safe_to_open"]
    assert any(b["code"] == "mapping_missing" for b in body["readiness"]["blockers"])


def test_open_week_end_to_end_through_the_route_materialises_matchups(finals_client):
    _built, bracket = _seed_bracket(finals_client, year=2602)
    response = finals_client.post(f"/api/admin/finals/{bracket.bracket_id}/weeks/1/open")
    assert response.status_code == 200
    body = response.json()
    assert body["round_state"] == "open"
    assert all(p["matchup_id"] is not None for p in body["pairings"] if p["slot"] != "bye")

    # A second open attempt is correctly rejected, not silently repeated.
    again = finals_client.post(f"/api/admin/finals/{bracket.bracket_id}/weeks/1/open")
    assert again.status_code == 409


def test_advance_and_rewind_routes_end_to_end(finals_client):
    _built, bracket = _seed_bracket(finals_client, year=2603)
    database = finals_client.app.state.database
    finals_client.post(f"/api/admin/finals/{bracket.bracket_id}/weeks/1/open")

    repo = FinalsBracketRepository(database)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(database, pairings["qf"].matchup_id, 100, 50)
    seed_official_result(database, pairings["ef"].matchup_id, 50, 100)

    advance = finals_client.post(
        f"/api/admin/finals/{bracket.bracket_id}/advance/1", params={"reason": "route advance"}
    )
    assert advance.status_code == 200
    assert len(repo.list_pairings(bracket.bracket_id, week_number=2)) == 2

    rewind_preview = finals_client.post(
        f"/api/admin/finals/{bracket.bracket_id}/rewind/1", params={"reason": "route rewind preview"}
    )
    assert rewind_preview.status_code == 200
    assert rewind_preview.json()["no_change_needed"]


def test_rewind_route_returns_409_with_report_when_blocked(finals_client):
    _built, bracket = _seed_bracket(finals_client, year=2604)
    database = finals_client.app.state.database
    finals_client.post(f"/api/admin/finals/{bracket.bracket_id}/weeks/1/open")
    repo = FinalsBracketRepository(database)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(database, pairings["qf"].matchup_id, 100, 50)
    seed_official_result(database, pairings["ef"].matchup_id, 50, 100)
    finals_client.post(f"/api/admin/finals/{bracket.bracket_id}/advance/1", params={"reason": "advance"})
    finals_client.post(f"/api/admin/finals/{bracket.bracket_id}/weeks/2/open")

    week2 = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=2)}
    seed_official_result(database, week2["first_semi"].matchup_id, 120, 40)

    from tests.finals_helpers import correct_official_result

    correct_official_result(database, pairings["ef"].matchup_id, 95, 60, reason="route correction")

    rewind = finals_client.post(
        f"/api/admin/finals/{bracket.bracket_id}/rewind/1", params={"reason": "should be blocked", "apply": True}
    )
    assert rewind.status_code == 409
    assert rewind.json()["detail"]["report"]["blocked"]
