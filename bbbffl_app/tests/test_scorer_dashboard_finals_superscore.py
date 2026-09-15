"""Issue #208: the Scorer Operations Dashboard's finals-week/SuperScore
composition. Before this existed, `app.scorer_dashboard.build_scorer_
dashboard` only ever read `ordinary` rounds at all -- requesting the
dashboard with a finals/SuperScore `round_id` silently fell through to
`select_current_round`'s ordinary fallback (the reported "resolves back to
Round 20" defect). `app.routes.scorer_dashboard` now resolves the
requested round's stream first and, for `finals`/`superscore`, composes
`app.finals_superscore_dashboard.build_finals_week_dashboard` instead --
this proves navigating to either the finals or the concurrent SuperScore
round_id reaches the *same* composed week, with no fabricated SuperScore
opponent, human-readable labels, and the existing finals/SuperScore
review/publish service URLs (never a generic ordinary one) -- while the
ordinary dashboard's own navigation is unaffected."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.authorization import Principal, Role
from app.finals import FinalsBracketRepository
from app.finals_preflight import open_finals_week
from app.superscore_round import confirm_afl_mapping, ensure_round, ensure_stream, open_round, setup_round
from tests.finals_helpers import KnownRound, accept_week_mapping, build_finals_ready_season

ACTOR = ActorContext.anonymous_operator("test")


def _open_finals_week1_and_superscore1(year=2404, database=None):
    built = build_finals_ready_season(year=year, database=database)
    database = built["database"]
    repo = FinalsBracketRepository(database)
    bracket = repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="issue #208 dashboard test bracket",
    )["bracket"]
    week1_round_id = repo.get_week_round_id(bracket.bracket_id, 1)
    accept_week_mapping(database, week1_round_id, year=year, afl_round_id=9001)
    open_finals_week(database, bracket.bracket_id, 1, actor=ACTOR)

    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (built["season"].season_id,)
    ).fetchone()
    stream = ensure_stream(
        database, built["season"].season_id, rules_row["rules_version_id"], built["ordinary_competition_id"]
    )
    validator = KnownRound({(year, 9001)})
    ss1_round_id = ensure_round(database, stream.competition_id, 1, 1)
    confirm_afl_mapping(database, validator, ss1_round_id, year, 9001, reason="SS1 concurrent with 2026 finals week 1")
    setup_round(database, ss1_round_id, reason="SS1 round setup")
    open_round(database, ss1_round_id, reason="open SS1")

    built["bracket"] = bracket
    built["week1_round_id"] = week1_round_id
    built["ss1_round_id"] = ss1_round_id
    return built


@pytest.fixture
def dashboard_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _seed(client, year):
    """Build the finals+SuperScore fixture directly on the running app's
    own database connection (exactly like `tests/test_scorer_dashboard_api.
    py`'s own `_seed` does for `round_review_helpers.full_round`) -- every
    `request.app.state.*` repository was constructed once, at app startup,
    bound to that one connection, so a *separate* fixture database would
    leave those repositories reading an empty database underneath it."""
    return _open_finals_week1_and_superscore1(year=year, database=client.app.state.database)


def _admin(client):
    from app.routes.scorer_dashboard import require_scorer_dashboard

    principal = Principal(Role.ADMIN, "admin-1", "Admin", session_id="s1")
    client.app.dependency_overrides[require_scorer_dashboard] = lambda: principal
    return principal


def test_navigating_to_finals_week1_round_id_does_not_fall_back_to_an_ordinary_round(dashboard_client):
    client = dashboard_client
    built = _seed(client, 9401)
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["week1_round_id"]}
    )
    assert response.status_code == 200, response.text
    dashboard = response.json()["dashboard"]
    assert dashboard["stream"] == "finals_week"
    assert dashboard["finals"]["round_id"] == built["week1_round_id"]
    # The historical defect: silently substituting whatever ordinary round
    # `select_current_round` would otherwise pick (Round 20 in the reported
    # case). This composed shape has no ordinary `round` at all.
    assert dashboard["round"] is None


def test_navigating_to_the_concurrent_superscore_round_id_composes_the_same_finals_week(dashboard_client):
    client = dashboard_client
    built = _seed(client, 9402)
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["ss1_round_id"]}
    )
    assert response.status_code == 200, response.text
    dashboard = response.json()["dashboard"]
    assert dashboard["stream"] == "finals_week"
    assert dashboard["superscore"]["round_id"] == built["ss1_round_id"]
    # Both streams resolved for the *same* AFL finals week, not just the one named.
    assert dashboard["finals"]["round_id"] == built["week1_round_id"]


def test_finals_section_shows_week1_matchups_and_bye_without_publishing_through_the_ordinary_boundary(
    dashboard_client,
):
    client = dashboard_client
    built = _seed(client, 9403)
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["week1_round_id"]}
    )
    finals = response.json()["dashboard"]["finals"]
    assert finals["available"] is True
    assert finals["week_label"] == "Finals Week 1"
    assert finals["bye"] is not None and finals["bye"]["team_name"]
    assert {m["slot"] for m in finals["matchups"]} == {"qf", "ef"}
    for matchup in finals["matchups"]:
        assert matchup["matchup_id"] is not None
        assert matchup["home_team_name"] and matchup["away_team_name"]
    # Existing finals-specific review/publish/progression services, never
    # the generic ordinary round-review sign-off boundary.
    assert finals["publish_url"] == f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/publish"
    assert (
        finals["advance_to_review_url"] == f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/advance-to-review"
    )


def test_superscore_section_lists_all_ten_entries_with_no_fabricated_opponent(dashboard_client):
    client = dashboard_client
    built = _seed(client, 9404)
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["ss1_round_id"]}
    )
    superscore = response.json()["dashboard"]["superscore"]
    assert superscore["available"] is True
    assert superscore["round_label"] == "SuperScore 1"
    assert superscore["entry_count"] == 10
    assert len(superscore["entries"]) == 10
    for entry in superscore["entries"]:
        assert "opponent" not in entry
        assert "home_team_name" not in entry and "away_team_name" not in entry
        assert entry["review_status"] in ("not_submitted", "submitted", "calculated", "stale_calculation", "published")
    # Existing SuperScore entry-scoped calculation/publication service, not
    # a UI-layer reimplementation.
    assert superscore["publish_url"] == f"/api/season-superscore/scorer/rounds/{built['ss1_round_id']}/publish"
    assert superscore["calculate_url"] == f"/api/season-superscore/scorer/rounds/{built['ss1_round_id']}/calculate"


def test_requesting_a_finals_round_from_a_different_season_fails_closed(dashboard_client):
    """Never trust `round_id` as authority over which season's data to
    compose -- a real finals round belonging to a *different* season than
    the resolved `season_id` must 404, not silently substitute anything."""
    client = dashboard_client
    built_a = _seed(client, 9405)
    # A second, genuinely different season/finals bracket in the *same*
    # database as the app's own state, so its round_id is a real,
    # resolvable finals round -- proving the season-ownership check itself,
    # not merely that an unknown round_id doesn't exist at all.
    built_b = _open_finals_week1_and_superscore1(year=9406, database=client.app.state.database)
    assert built_a["season"].season_id != built_b["season"].season_id

    _admin(client)
    response = client.get(
        "/api/scorer/dashboard",
        params={"season_id": built_a["season"].season_id, "round_id": built_b["week1_round_id"]},
    )
    assert response.status_code == 404


def test_ordinary_dashboard_navigation_is_unaffected(dashboard_client):
    """No `round_id`, or an ordinary one, still reaches the unchanged
    ordinary `app.scorer_dashboard.build_scorer_dashboard` read model."""
    client = dashboard_client
    built = _seed(client, 9407)
    _admin(client)

    response = client.get("/api/scorer/dashboard", params={"season_id": built["season"].season_id})
    assert response.status_code == 200, response.text
    dashboard = response.json()["dashboard"]
    assert dashboard.get("stream") != "finals_week"
    assert dashboard["round"] is not None
    assert "finals" not in dashboard
    assert len(dashboard["lineups"]) == 10
