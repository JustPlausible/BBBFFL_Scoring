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
from app.coach_lineup import CoachLineupService
from app.db import transaction
from app.finals import FinalsBracketRepository
from app.finals_preflight import open_finals_week
from app.superscore_results import SuperScoreLeaderboardService
from app.superscore_round import (
    advance_round_to_review,
    confirm_afl_mapping,
    ensure_round,
    ensure_stream,
    open_round,
    setup_round,
)
from tests.finals_helpers import KnownRound, accept_week_mapping, build_finals_ready_season

ACTOR = ActorContext.anonymous_operator("test")


class _StubAflClient:
    def get_matches(self, afl_round_id):
        return []

    def get_rounds(self, afl_season_id):
        return []


def _coach_id(database, season_entry_id):
    row = database.execute(
        "SELECT coach_id FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
        (season_entry_id,),
    ).fetchone()
    return row["coach_id"]


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


def test_finals_open_week_url_is_the_paired_endpoint_when_a_concurrent_superscore_round_exists(dashboard_client):
    """Issue #211 P1 (Codex review): the dashboard's own "Open finals week"
    button must reach workflow B's paired action
    (`app.finals_superscore_open.open_finals_and_superscore_week`), not
    silently continue opening Finals alone while a concurrent SuperScore
    round sits unopened and unsynchronised."""
    client = dashboard_client
    built = _seed(client, 9411)
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["week1_round_id"]}
    )
    finals = response.json()["dashboard"]["finals"]
    assert finals["open_week_url"] == f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/open-paired"
    # `_seed` fully opens both streams -- SuperScore's own open is not
    # pending here, so the (paired) button need not stay forced-visible.
    assert finals["superscore_open_pending"] is False


def test_finals_section_keeps_superscore_open_pending_true_after_finals_opens_standalone_first(dashboard_client):
    """Issue #211 P1 (Codex review, round 2): `open_finals_and_superscore_
    week` explicitly supports retrying just the SuperScore half once
    Finals has already opened -- the dashboard payload must keep reporting
    that a paired open is still pending so the UI's retry button stays
    visible, not only while Finals itself reads not_created/upcoming."""
    client = dashboard_client
    database = client.app.state.database
    built = build_finals_ready_season(year=9413, database=database)
    repo = FinalsBracketRepository(database)
    bracket = repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="issue #211 pending-superscore dashboard test bracket",
    )["bracket"]
    week1_round_id = repo.get_week_round_id(bracket.bracket_id, 1)
    accept_week_mapping(database, week1_round_id, year=9413, afl_round_id=9001)
    open_finals_week(database, bracket.bracket_id, 1, actor=ACTOR)

    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (built["season"].season_id,)
    ).fetchone()
    stream = ensure_stream(
        database, built["season"].season_id, rules_row["rules_version_id"], built["ordinary_competition_id"]
    )
    ensure_round(database, stream.competition_id, 1, 1)  # configured, but never set up/opened
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": week1_round_id}
    )
    finals = response.json()["dashboard"]["finals"]
    assert finals["lifecycle_state"] == "open"
    assert finals["superscore_open_pending"] is True
    assert finals["open_week_url"] == f"/api/admin/finals/{bracket.bracket_id}/weeks/1/open-paired"


def test_finals_open_week_url_falls_back_to_the_standalone_endpoint_without_a_concurrent_superscore_round(
    dashboard_client,
):
    """No SuperScore round is configured for this week at all -- pairing is
    impossible, so the dashboard must keep using the standalone finals-only
    open action rather than a paired endpoint that would only ever fail."""
    client = dashboard_client
    database = client.app.state.database
    built = build_finals_ready_season(year=9412, database=database)
    repo = FinalsBracketRepository(database)
    bracket = repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="issue #211 no-superscore dashboard test bracket",
    )["bracket"]
    week1_round_id = repo.get_week_round_id(bracket.bracket_id, 1)
    accept_week_mapping(database, week1_round_id, year=9412, afl_round_id=9001)
    open_finals_week(database, bracket.bracket_id, 1, actor=ACTOR)
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": week1_round_id}
    )
    finals = response.json()["dashboard"]["finals"]
    assert finals["available"] is True
    assert finals["open_week_url"] == f"/api/admin/finals/{bracket.bracket_id}/weeks/1/open"


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


def test_published_entry_shows_the_frozen_leaderboard_total_not_a_later_mutable_recalculation(dashboard_client):
    """Issue #208 review finding (P2): once an entry is published, its
    `rank`/`is_joint_winner` already come from the frozen leaderboard row --
    but `total_score` was still read from the *mutable* `superscore_entry_
    calculation` row, which a later recalculation (e.g. after a post-
    publish DNP ruling correction) can change without touching the
    published leaderboard. That paired a frozen rank with a score that no
    longer matches it. `total_score` must come from the same frozen
    leaderboard row as `rank` whenever the entry is published."""
    client = dashboard_client
    built = _seed(client, 9410)
    database = built["database"]

    service = CoachLineupService(database, afl_client=_StubAflClient())
    for entry_obj in built["entries"]:
        coach_id = _coach_id(database, entry_obj.season_entry_id)
        entry = service.resolve(coach_id, built["season"].season_id, built["ss1_round_id"])
        draft = service.ensure_draft(built["season"].season_id, built["ss1_round_id"], entry)
        service.submit(draft, submission_version=0, coach_id=coach_id)

    advance_round_to_review(database, built["ss1_round_id"], actor=ACTOR, reason="advance for publish")
    leaderboard_service = SuperScoreLeaderboardService(database, _StubAflClient())
    published = leaderboard_service.publish(built["ss1_round_id"], actor=ACTOR, reason="publish for test")
    published_entry = published["entries"][0]
    entry_id = published_entry["season_entry_id"]
    published_total_score = published_entry["total_score"]

    # Simulate a later, unpublished recalculation drifting the mutable
    # calculation row's score away from the frozen, already-published
    # leaderboard total -- e.g. a post-publish DNP correction that hasn't
    # (yet) been re-published.
    drifted_score = published_total_score + 37.5
    with transaction(database) as conn:
        conn.execute(
            "UPDATE superscore_entry_calculation SET total_score=? WHERE bbbffl_round_id=? AND season_entry_id=?",
            (drifted_score, built["ss1_round_id"], entry_id),
        )

    _admin(client)
    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["ss1_round_id"]}
    )
    assert response.status_code == 200, response.text
    entries = {e["season_entry_id"]: e for e in response.json()["dashboard"]["superscore"]["entries"]}
    entry = entries[entry_id]
    assert entry["review_status"] == "published"
    assert entry["total_score"] == published_total_score
    assert entry["total_score"] != drifted_score


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


def test_replay_operator_is_told_open_finals_week_is_not_actionable_for_them(dashboard_client):
    """Issue #208 review finding (P2): `open_week` (`app.routes.
    finals_preflight.open_week`) requires `roundsetup.manage` -- a Replay
    Operator holds `round.review` (enough to reach this dashboard at all)
    but not `roundsetup.manage`, so the composed dashboard must say so
    rather than advertise a button that will 403, mirroring the ordinary
    dashboard's existing `actionable_by_you` convention for next-action/
    attention items."""
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboard_client
    built = _seed(client, 9408)
    operator = Principal(Role.REPLAY_OPERATOR, display_name="Operator")
    client.app.dependency_overrides[require_scorer_dashboard] = lambda: operator

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["week1_round_id"]}
    )
    assert response.status_code == 200, response.text
    finals = response.json()["dashboard"]["finals"]
    assert finals["available"] is True
    assert finals["open_week_actionable_by_you"] is False


def test_scorer_is_told_open_finals_week_is_actionable_for_them(dashboard_client):
    """The counterpart to the Replay Operator case above: a Scorer *does*
    hold `roundsetup.manage`, so the same week must be presented as
    actionable, never withheld just because gating exists at all."""
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboard_client
    built = _seed(client, 9409)
    scorer = Principal(Role.SCORER, display_name="Scorer")
    client.app.dependency_overrides[require_scorer_dashboard] = lambda: scorer

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["week1_round_id"]}
    )
    assert response.status_code == 200, response.text
    finals = response.json()["dashboard"]["finals"]
    assert finals["open_week_actionable_by_you"] is True


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
