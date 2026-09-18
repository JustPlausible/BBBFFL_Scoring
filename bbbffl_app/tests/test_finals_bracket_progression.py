"""Issue #216: HTTP/domain regression coverage for surfacing the existing
Finals bracket-advance preview/apply path (issue #190/#201's
`FinalsBracketRepository.preview_advance_bracket`/`advance_bracket`,
previously reachable only from the CLI) in the Scorer UI. Builds on
tests/test_finals.py's own bracket-setup helpers and tests/
test_finals_routes.py's own HTTP fixture, rather than inventing a second
finals-bracket test convention."""

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.finals import FinalsBracketRepository
from app.finals_preflight import open_finals_week
from app.finals_superscore_dashboard import build_finals_progression_preview
from app.identity import IdentityRepository
from tests.finals_helpers import correct_official_result, seed_official_result
from tests.test_finals import (
    ACTOR,
    _advance_week1,
    _bracket_with_mappings,
    _open_and_seed_week1,
    _open_and_seed_week2,
    _repo,
)
from tests.test_finals_routes import _seed_bracket


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


# -- Domain: build_finals_progression_preview --------------------------------


def test_preview_reports_human_readable_pairings_and_expected_versions():
    built, bracket = _bracket_with_mappings(year=2701)
    _open_and_seed_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    repo = _repo(built)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    identities = IdentityRepository(built["database"])

    preview = build_finals_progression_preview(built["database"], identities, built["season"], bracket.bracket_id, 1)
    assert preview["ready"] is True
    assert preview["diagnostic"] is None
    assert preview["from_week_label"] == "Finals Week 1"
    assert preview["target_week_label"] == "Finals Week 2"
    assert {p["slot"] for p in preview["new_pairings"]} == {"second_semi", "first_semi"}
    for pairing in preview["new_pairings"]:
        assert pairing["home_team_name"]
        assert pairing["away_team_name"]
        assert pairing["slot_label"]
    assert preview["expected_versions"] == {pairings["qf"].matchup_id: 1, pairings["ef"].matchup_id: 1}
    # The Elimination Final's loser is eliminated outright at Week 1 -> 2
    # (only the Qualifying Final's loser drops into the lower bracket).
    assert preview["elimination"] is not None
    assert preview["elimination"]["stage"] == "week1_elimination_final"
    assert preview["elimination"]["team_name"]


def test_preview_reports_elimination_with_team_name():
    built, bracket = _bracket_with_mappings(year=2702)
    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    _open_and_seed_week2(built, bracket, ss2_result=(100, 50), fs_result=(50, 100))
    repo = _repo(built)
    week2 = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=2)}
    identities = IdentityRepository(built["database"])

    preview = build_finals_progression_preview(built["database"], identities, built["season"], bracket.bracket_id, 2)
    assert preview["ready"] is True
    assert preview["elimination"] is not None
    assert preview["elimination"]["season_entry_id"] == week2["first_semi"].home_season_entry_id
    assert preview["elimination"]["team_name"]
    assert preview["elimination"]["stage"] == "week2_first_semi_final"


def test_preview_reports_a_diagnostic_instead_of_raising_when_the_source_week_is_incomplete():
    built, bracket = _bracket_with_mappings(year=2703)
    open_finals_week(built["database"], bracket.bracket_id, 1, actor=ACTOR)
    identities = IdentityRepository(built["database"])

    preview = build_finals_progression_preview(built["database"], identities, built["season"], bracket.bracket_id, 1)
    assert preview["ready"] is False
    assert preview["diagnostic"] is not None
    assert "no published official result" in preview["diagnostic"]
    assert preview["new_pairings"] == []
    assert preview["expected_versions"] is None


# -- HTTP: preview route, apply route's target_round_id, stale rejection ----


def test_preview_route_returns_the_domain_preview_report(finals_client):
    _built, bracket = _seed_bracket(finals_client, year=2704)
    database = finals_client.app.state.database
    finals_client.post(f"/api/admin/finals/{bracket.bracket_id}/weeks/1/open")
    repo = FinalsBracketRepository(database)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(database, pairings["qf"].matchup_id, 100, 50)
    seed_official_result(database, pairings["ef"].matchup_id, 50, 100)

    response = finals_client.get(f"/api/admin/finals/{bracket.bracket_id}/advance/1/preview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready"] is True
    assert len(body["new_pairings"]) == 2
    assert body["target_week_label"] == "Finals Week 2"


def test_preview_route_404s_for_an_unknown_bracket(finals_client):
    response = finals_client.get("/api/admin/finals/does-not-exist/advance/1/preview")
    assert response.status_code == 404


def test_apply_route_response_includes_the_next_weeks_round_id_for_direct_navigation(finals_client):
    _built, bracket = _seed_bracket(finals_client, year=2705)
    database = finals_client.app.state.database
    finals_client.post(f"/api/admin/finals/{bracket.bracket_id}/weeks/1/open")
    repo = FinalsBracketRepository(database)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(database, pairings["qf"].matchup_id, 100, 50)
    seed_official_result(database, pairings["ef"].matchup_id, 50, 100)

    response = finals_client.post(f"/api/admin/finals/{bracket.bracket_id}/advance/1", params={"reason": "advance"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target_round_id"] == repo.get_week_round_id(bracket.bracket_id, 2)
    assert body["target_week"] == 2


def test_apply_after_preview_with_an_intervening_correction_is_rejected_as_stale_not_silently_applied(finals_client):
    """The exact "operator previews, a correction lands, operator confirms"
    race issue #216's acceptance criteria requires stay protected: the
    Scorer UI's confirm step re-sends the preview's own `expected_versions`
    (issue #201), so a correction landing in between must still be
    rejected with 409, never silently applied against a superseded result."""
    _built, bracket = _seed_bracket(finals_client, year=2706)
    database = finals_client.app.state.database
    finals_client.post(f"/api/admin/finals/{bracket.bracket_id}/weeks/1/open")
    repo = FinalsBracketRepository(database)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(database, pairings["qf"].matchup_id, 100, 50)
    seed_official_result(database, pairings["ef"].matchup_id, 50, 100)

    preview = finals_client.get(f"/api/admin/finals/{bracket.bracket_id}/advance/1/preview").json()
    assert preview["ready"] is True

    correct_official_result(database, pairings["qf"].matchup_id, 90, 60, reason="late correction before confirm")

    apply = finals_client.post(
        f"/api/admin/finals/{bracket.bracket_id}/advance/1",
        params={"reason": "advance after stale preview", "expected_versions": json.dumps(preview["expected_versions"])},
    )
    assert apply.status_code == 409
    assert len(repo.list_pairings(bracket.bracket_id, week_number=2)) == 0


def test_preview_then_apply_with_the_preview_own_expected_versions_succeeds(finals_client):
    _built, bracket = _seed_bracket(finals_client, year=2707)
    database = finals_client.app.state.database
    finals_client.post(f"/api/admin/finals/{bracket.bracket_id}/weeks/1/open")
    repo = FinalsBracketRepository(database)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(database, pairings["qf"].matchup_id, 100, 50)
    seed_official_result(database, pairings["ef"].matchup_id, 50, 100)

    preview = finals_client.get(f"/api/admin/finals/{bracket.bracket_id}/advance/1/preview").json()
    apply = finals_client.post(
        f"/api/admin/finals/{bracket.bracket_id}/advance/1",
        params={
            "reason": "scorer-confirmed progression",
            "expected_versions": json.dumps(preview["expected_versions"]),
        },
    )
    assert apply.status_code == 200, apply.text
    assert len(repo.list_pairings(bracket.bracket_id, week_number=2)) == 2
