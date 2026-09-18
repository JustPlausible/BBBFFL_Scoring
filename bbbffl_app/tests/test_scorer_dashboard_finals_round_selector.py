"""Issue #219: the Scorer Round selector's "selected" round must always
match the round the dashboard actually resolved and rendered -- for direct
navigation to a Finals week via `season_id` + `round_id`, for post-
progression navigation (the round the bracket-advance apply route hands
back as `target_round_id`), and for ordinary Rounds 1-20 (unaffected,
regression only).

`season_round_options` (`app.scorer_dashboard`) previously scoped its
Finals-week rows by `finals_bracket.season_id` directly; `build_finals_
week_dashboard`'s own season check (and `round_stream_type`, its shared
authority) instead derive a round's season through its own `competition_
stream` row. The fix re-scopes `season_round_options`'s Finals rows (and
the composed dashboard's own superscore-side bracket lookup) through that
identical `competition_stream` join, so the two can never independently
disagree about which season a Finals week's round belongs to -- the one
concrete way the selector's `bbbffl_round_id` and the dashboard's own
resolved round could diverge while the page itself still rendered
correctly."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.authorization import Principal, Role
from app.finals import FinalsBracketRepository
from app.finals_preflight import open_finals_week
from tests.test_finals import ACTOR, _advance_to_week3, _advance_week1, _bracket_with_mappings


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


def _admin(client):
    from app.routes.scorer_dashboard import require_scorer_dashboard

    principal = Principal(Role.ADMIN, "admin-1", "Admin", session_id="s1")
    client.app.dependency_overrides[require_scorer_dashboard] = lambda: principal
    return principal


def _selected_option(round_options, selected_round_id):
    return next((o for o in round_options if o["bbbffl_round_id"] == selected_round_id), None)


def test_direct_navigation_to_preliminary_final_selects_it_in_the_round_selector(dashboard_client):
    _admin(dashboard_client)
    database = dashboard_client.app.state.database
    built, bracket = _bracket_with_mappings(year=9601, database=database)
    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    _advance_to_week3(built, bracket, ss2_result=(100, 50), fs_result=(100, 50))
    week3_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 3)
    open_finals_week(database, bracket.bracket_id, 3, actor=ACTOR)

    response = dashboard_client.get(
        f"/api/scorer/dashboard?season_id={built['season'].season_id}&round_id={week3_round_id}"
    )
    assert response.status_code == 200, response.text
    dashboard = response.json()["dashboard"]
    assert dashboard["stream"] == "finals_week"
    assert dashboard["finals"]["round_id"] == week3_round_id

    selected = _selected_option(dashboard["round_options"], week3_round_id)
    assert selected is not None, "Preliminary Final's own round_id must appear in round_options"
    assert selected["round_label"] == "Preliminary Final"
    assert selected["stream"] == "finals"


def test_direct_navigation_to_grand_final_selects_it_in_the_round_selector(dashboard_client):
    _admin(dashboard_client)
    database = dashboard_client.app.state.database
    built, bracket = _bracket_with_mappings(year=9602, database=database)
    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    _advance_to_week3(built, bracket, ss2_result=(100, 50), fs_result=(100, 50))
    week4_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 4)
    # Grand Final's round is materialised at bracket creation (every week's
    # round exists up front, issue #216) even though its pairing isn't
    # derived until Week 3 publishes -- direct navigation to it before that
    # must still select it correctly (its own round_options label reports
    # `not_created` state, as a real early click-through would see).

    response = dashboard_client.get(
        f"/api/scorer/dashboard?season_id={built['season'].season_id}&round_id={week4_round_id}"
    )
    assert response.status_code == 200, response.text
    dashboard = response.json()["dashboard"]
    assert dashboard["stream"] == "finals_week"
    assert dashboard["finals"]["round_id"] == week4_round_id

    selected = _selected_option(dashboard["round_options"], week4_round_id)
    assert selected is not None, "Grand Final's own round_id must appear in round_options"
    assert selected["round_label"] == "Grand Final"


def test_post_progression_navigation_to_preliminary_final_selects_it(dashboard_client):
    """Mirrors the Scorer UI's own post-progression flow
    (`wireProgressionApply` in `scorer_dashboard.html`): after confirming
    "Advance bracket", the client navigates using the apply route's own
    `target_round_id`, never a value it derived independently."""
    _admin(dashboard_client)
    database = dashboard_client.app.state.database
    built, bracket = _bracket_with_mappings(year=9603, database=database)
    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))

    from tests.test_finals import _open_and_seed_week2, _repo

    _open_and_seed_week2(built, bracket, ss2_result=(100, 50), fs_result=(100, 50))
    repo = _repo(built)
    week2_pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=2)}
    expected_versions = {
        week2_pairings["second_semi"].matchup_id: 1,
        week2_pairings["first_semi"].matchup_id: 1,
    }
    advance_result = repo.advance_bracket(
        bracket.bracket_id, 2, actor=ACTOR, reason="advance from week 2", expected_versions=expected_versions
    )
    assert advance_result["target_week"] == 3
    target_round_id = repo.get_week_round_id(bracket.bracket_id, 3)

    response = dashboard_client.get(
        f"/api/scorer/dashboard?season_id={built['season'].season_id}&round_id={target_round_id}"
    )
    assert response.status_code == 200, response.text
    dashboard = response.json()["dashboard"]
    selected = _selected_option(dashboard["round_options"], target_round_id)
    assert selected is not None
    assert selected["round_label"] == "Preliminary Final"


def test_ordinary_round_selector_navigation_is_unaffected(dashboard_client):
    """Regression only: an ordinary round's own selector-driven navigation
    (a `round_options` entry's `bbbffl_round_id` fed straight back into the
    next request, exactly like `scorer_dashboard.html`'s `roundSelect.
    onchange`) must still select the round actually resolved."""
    _admin(dashboard_client)
    database = dashboard_client.app.state.database
    built, _bracket = _bracket_with_mappings(year=9604, database=database)

    first = dashboard_client.get(f"/api/scorer/dashboard?season_id={built['season'].season_id}")
    assert first.status_code == 200, first.text
    round_options = first.json()["dashboard"]["round_options"]
    round5 = next(o for o in round_options if o["round_label"] == "Round 5")

    response = dashboard_client.get(
        f"/api/scorer/dashboard?season_id={built['season'].season_id}&round_id={round5['bbbffl_round_id']}"
    )
    assert response.status_code == 200, response.text
    dashboard = response.json()["dashboard"]
    assert dashboard["round"]["bbbffl_round_id"] == round5["bbbffl_round_id"]
    selected = _selected_option(dashboard["round_options"], round5["bbbffl_round_id"])
    assert selected is not None
    assert selected["round_label"] == "Round 5"


def test_season_round_options_scopes_finals_rows_through_the_round_competition_stream_not_finals_bracket_season_id(
    dashboard_client,
):
    """Issue #219, direct unit coverage of the fix itself: `season_round_
    options` must keep listing a season's Finals weeks even when `finals_
    bracket.season_id` itself is corrupted/diverged from the bracket's
    rounds' own authoritative `competition_stream.season_id` -- the exact
    shape of inconsistency that left the round selector's `bbbffl_round_id`
    unmatched (silently falling back to the browser's own "first option"
    default) while the page itself still rendered the correct Finals week,
    since `build_finals_week_dashboard` resolves a Finals round directly by
    `bbbffl_round_id`, never through `finals_bracket.season_id`."""
    from app.scorer_dashboard import season_round_options
    from app.season import SeasonRepository

    database = dashboard_client.app.state.database
    built, bracket = _bracket_with_mappings(year=9605, database=database)
    season_id = built["season"].season_id

    before = season_round_options(database, season_id)
    finals_labels_before = {o["round_label"] for o in before if o["stream"] == "finals"}
    assert finals_labels_before == {"Finals Week 1", "Finals Week 2", "Preliminary Final", "Grand Final"}

    # A real, unrelated season -- satisfies finals_bracket.season_id's own
    # foreign key while still diverging from the bracket's rounds' actual
    # competition_stream.season_id, exactly the inconsistency class the fix
    # closes (never reachable through any legitimate domain operation, but
    # exactly what `finals_bracket.season_id` being trusted as authority
    # over the selector's own listing would silently mis-scope against).
    other_season = SeasonRepository(database).create_season(9606, "Unrelated season")
    database.execute(
        "UPDATE finals_bracket SET season_id=? WHERE bracket_id=?", (other_season.season_id, bracket.bracket_id)
    )

    after = season_round_options(database, season_id)
    finals_labels_after = {o["round_label"] for o in after if o["stream"] == "finals"}
    assert finals_labels_after == finals_labels_before, (
        "season_round_options must not lose a season's Finals weeks merely because finals_bracket.season_id "
        "diverges from the round's own competition_stream.season_id"
    )
