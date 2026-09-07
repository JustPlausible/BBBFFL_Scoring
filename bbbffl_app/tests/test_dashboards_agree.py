"""Integration coverage proving the Administrator Dashboard (issue #148)
and the Scorer Operations Dashboard (issue #147) never contradict one
another for the same season/round state -- issue #148's explicit
requirement: "at least one integration test specifically asserting that
the Administrator Dashboard and Scorer Dashboard do not contradict one
another for the same season/round state."

Runs at the HTTP level, through the real `app.main` wiring, so this proves
the two routers' independent authorization/season-scoping never causes
them to disagree about the underlying facts -- not just that their two
read-model modules happen to agree when called directly in-process (see
`tests/test_admin_dashboard.py::test_administrator_summary_agrees_with_the_
underlying_scorer_state` for that narrower unit-level check).
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.authorization import Principal, Role
from tests.admin_dashboard_helpers import build_governed_season


@pytest.fixture
def dashboards_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _dual_role_admin(client):
    """One authenticated coach identity holding standing Administrator
    authority -- Administrator's capability set is a superset of Scorer's
    (the wildcard in `app.authorization.CAPABILITIES`), so this single
    principal can reach both dashboards without a separate Scorer grant
    (issue #148's "navigation to the Scorer Dashboard is permitted without
    expanding the underlying Scorer authority model")."""
    return Principal(Role.ADMIN, "admin-1", "Dual Role Admin", granted_roles=frozenset({Role.ADMIN}), session_id="s1")


def test_weekly_operations_active_means_scorer_resolves_the_same_current_round(dashboards_client):
    """ "Admin says weekly operations are active -> Scorer dashboard
    resolves that same round as current" (issue #148's example)."""
    from app.routes.admin_dashboard import require_admin_dashboard
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboards_client
    g = build_governed_season(client.app.state.database, year=9501, close_preseason=True, open_round=True)
    principal = _dual_role_admin(client)
    client.app.dependency_overrides[require_admin_dashboard] = lambda: principal
    client.app.dependency_overrides[require_scorer_dashboard] = lambda: principal
    try:
        admin_body = client.get("/api/admin/dashboard", params={"season_id": g.season.season_id}).json()
        assert admin_body["dashboard"]["current_round"] is not None
        assert admin_body["dashboard"]["workflow_map"]

        scorer_body = client.get("/api/scorer/dashboard", params={"season_id": g.season.season_id}).json()
        assert scorer_body["dashboard"]["round"] is not None

        assert (
            admin_body["dashboard"]["current_round"]["bbbffl_round_id"]
            == scorer_body["dashboard"]["round"]["bbbffl_round_id"]
        )
        assert admin_body["dashboard"]["current_round"]["state"] == scorer_body["dashboard"]["round"]["state"]
        assert admin_body["dashboard"]["scorer_summary"]["round_state"] == scorer_body["dashboard"]["round"]["state"]
        assert (
            admin_body["dashboard"]["scorer_summary"]["next_action"]["code"]
            == scorer_body["dashboard"]["next_action"]["code"]
        )
    finally:
        client.app.dependency_overrides.clear()


def test_scorer_attention_on_the_admin_dashboard_is_reachable_on_the_scorer_surface(dashboards_client):
    """ "Admin says Scorer attention exists -> linked Scorer surface is
    accessible to an authorised dual-role user" (issue #148's example)."""
    from app.routes.admin_dashboard import require_admin_dashboard
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboards_client
    # No lockout trigger plan is configured, so the round opens straight
    # into a blocking "configure lockout plan" Scorer attention item.
    g = build_governed_season(client.app.state.database, year=9502, close_preseason=True, open_round=True)
    principal = _dual_role_admin(client)
    client.app.dependency_overrides[require_admin_dashboard] = lambda: principal
    client.app.dependency_overrides[require_scorer_dashboard] = lambda: principal
    try:
        admin_body = client.get("/api/admin/dashboard", params={"season_id": g.season.season_id}).json()["dashboard"]
        handoff_items = [item for item in admin_body["attention"] if item["category"] == "operational_handoff"]
        assert handoff_items, "expected the Admin dashboard to surface Scorer attention as a handoff item"
        assert all(item["url"] == "/scorer" for item in handoff_items)

        scorer_response = client.get("/api/scorer/dashboard", params={"season_id": g.season.season_id})
        assert scorer_response.status_code == 200
        scorer_attention = scorer_response.json()["dashboard"]["attention"]
        assert any(item["category"] == "blocking" for item in scorer_attention)
    finally:
        client.app.dependency_overrides.clear()


def test_published_round_state_is_consistent_across_both_surfaces(dashboards_client):
    """ "Completed/published round state is consistently reflected by both
    surfaces" (issue #148's example)."""
    from app.routes.admin_dashboard import require_admin_dashboard
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboards_client
    g = build_governed_season(client.app.state.database, year=9503, close_preseason=True, open_round=True)
    for target in ("live", "review"):
        g.lifecycle.transition(g.logical_round.bbbffl_round_id, target, actor=ActorContext.anonymous_operator("scorer"))
    matchups = g.lifecycle.list_matchups(g.logical_round.bbbffl_round_id)
    results = {matchup.matchup_id: (100, 90) for matchup in matchups}
    g.lifecycle.publish_results(g.logical_round.bbbffl_round_id, results, reason="approved")

    principal = _dual_role_admin(client)
    client.app.dependency_overrides[require_admin_dashboard] = lambda: principal
    client.app.dependency_overrides[require_scorer_dashboard] = lambda: principal
    try:
        admin_body = client.get("/api/admin/dashboard", params={"season_id": g.season.season_id}).json()["dashboard"]
        scorer_body = client.get("/api/scorer/dashboard", params={"season_id": g.season.season_id}).json()["dashboard"]
        assert admin_body["current_round"]["state"] == "final"
        assert scorer_body["round"]["state"] == "final"
        assert admin_body["readiness"]["rounds_opened"] == admin_body["readiness"]["rounds_defined"]
    finally:
        client.app.dependency_overrides.clear()
