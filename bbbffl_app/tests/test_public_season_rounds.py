"""Public season landing page / round browser (issue #161): the round
index, default-round selection, previous/next navigation, explicit round
selection, future-round previews, published-round rendering, matchup
links, the historical (as-of-published-round) ladder, canonical URLs, and
the public/private boundary. Built entirely on the existing fixture/
lifecycle/round-review/ladder services (`app.public_rounds`) -- see
tests/test_round_review_api.py for the pre-existing per-round Round Centre
coverage this complements rather than duplicates.

Uses its own isolated SQLite database, exactly like
tests/test_round_review_api.py, so it cannot contaminate any other test's
state.
"""

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_competition_lifecycle import configured, operational, progress_to_review, scores


@pytest.fixture
def season_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _publish_round_one(database, year, afl_round=100):
    """A ten-team, twenty-round ordinary season (the shared test helpers'
    default `regular_season_round_count`) with only Round 1 opened and
    published -- rounds 2-20 stay pure fixture-draw previews, exactly the
    "opened round" vs. "future round" split the public round browser must
    distinguish."""
    lifecycle, round_one, entries = operational(database, year, afl_round)
    progress_to_review(lifecycle, round_one.bbbffl_round_id)
    lifecycle.publish_results(round_one.bbbffl_round_id, scores(lifecycle, round_one.bbbffl_round_id))
    round_row = lifecycle.get_round(round_one.bbbffl_round_id)
    return round_row, entries


def test_round_index_lists_every_fixture_round_with_definitions_layered_on(season_client):
    client = season_client
    round_row, _ = _publish_round_one(client.app.state.database, 9001)
    season_id = round_row.season_id

    resp = client.get(f"/api/public/seasons/{season_id}/rounds")
    assert resp.status_code == 200
    body = resp.json()
    assert body["season_id"] == season_id
    assert "competition_id" not in body  # internal join key, never public
    assert len(body["rounds"]) == 20
    assert body["default_round_number"] == 1

    first = body["rounds"][0]
    assert first == {
        "round_number": 1,
        "label": "Round 1",
        "round_id": round_row.bbbffl_round_id,
        "state": "final",
        "published": True,
    }
    later = body["rounds"][5]
    assert later["round_number"] == 6
    assert later["label"] == "Round 6"
    assert later["round_id"] is None
    assert later["state"] == "scheduled"
    assert later["published"] is False


def test_default_round_is_the_most_recently_published_round(season_client):
    client = season_client
    round_row, _ = _publish_round_one(client.app.state.database, 9002)
    season_id = round_row.season_id

    index = client.get(f"/api/public/seasons/{season_id}/rounds").json()
    assert index["default_round_number"] == 1

    overview = client.get(f"/seasons/{season_id}", follow_redirects=False)
    assert overview.status_code == 302
    assert overview.headers["location"] == f"/seasons/{season_id}/rounds/1"


def test_default_round_prefers_an_opened_round_still_in_progress_over_a_published_one(season_client):
    client = season_client
    database = client.app.state.database
    round_one, _ = _publish_round_one(database, 9003)
    season_id = round_one.season_id

    # Round 2 is opened (created + transitioned) but not yet published --
    # it is "current" even though Round 1 is the most recently published.
    from app.competition_lifecycle import CompetitionLifecycleRepository
    from app.season import SeasonRepository

    seasons = SeasonRepository(database)
    lifecycle = CompetitionLifecycleRepository(database)
    round_two_def = seasons.create_round(round_one.competition_id, "round-2", "Round 2", 2)
    from app.round_mapping import RoundMappingRepository

    class _KnownRound:
        def round_exists(self, season, round_):
            return (season, round_) == (9003, 101)

    RoundMappingRepository(database).accept(round_two_def.bbbffl_round_id, 9003, 101, _KnownRound())
    lifecycle.create_ordinary_round(round_two_def.bbbffl_round_id)
    lifecycle.transition(round_two_def.bbbffl_round_id, "open")

    index = client.get(f"/api/public/seasons/{season_id}/rounds").json()
    assert index["default_round_number"] == 2


def test_unopened_round_definition_is_never_treated_as_in_progress_or_linkable(season_client):
    """Regression (Codex review, PR #171): a season that pre-creates every
    logical `bbbffl_round` definition up front (e.g. a first-half replay
    bootstrap) must not have those bare definitions -- no lifecycle row,
    hence never opened -- mistaken for "in progress", nor have their
    internal id handed out as a `round_id` a matchup card would link to
    (that link would 404 against the detailed view, since no lifecycle
    round exists yet)."""
    client = season_client
    database = client.app.state.database
    round_one, _ = _publish_round_one(database, 9021)
    season_id = round_one.season_id

    from app.season import SeasonRepository

    SeasonRepository(database).create_round(round_one.competition_id, "round-2", "Round 2", 2)

    index = client.get(f"/api/public/seasons/{season_id}/rounds").json()
    assert index["default_round_number"] == 1  # Round 1 stays current, not merely-defined Round 2
    round_two_summary = index["rounds"][1]
    assert round_two_summary["round_number"] == 2
    assert round_two_summary["round_id"] is None
    assert round_two_summary["state"] == "scheduled"

    preview = client.get(f"/api/public/seasons/{season_id}/rounds/2").json()
    assert preview["round_id"] is None  # nothing to link a matchup card to yet

    overview = client.get(f"/seasons/{season_id}", follow_redirects=False)
    assert overview.headers["location"] == f"/seasons/{season_id}/rounds/1"


def test_unfrozen_fixture_draft_never_leaks_as_a_public_preview(season_client):
    """Regression (Codex review, PR #171): FixtureRepository.list_matchups
    does not filter by draw state, so a round preview must check the draw
    is frozen itself -- otherwise an operator's still-mutable draft
    pairings would be shown to spectators as "the scheduled fixture" and
    could silently change underneath them before the draw is frozen."""
    client = season_client
    database = client.app.state.database
    from app.fixtures import FixtureRepository
    from app.identity import IdentityRepository
    from app.season import SeasonRepository

    seasons = SeasonRepository(database)
    identities = IdentityRepository(database)
    fixtures = FixtureRepository(database)

    season = seasons.create_season(9022, "9022")
    rules = seasons.create_rules_version(season.season_id, "ordinary", 1, "Rules")
    seasons.create_competition(season.season_id, rules.rules_version_id, "ordinary", "Ordinary", "ordinary")
    entries = []
    for number in range(10):
        coach = identities.create_coach(f"Draft Coach {number}")
        entries.append(
            identities.create_entry(season.season_id, f"draft-licence-{number}", coach.coach_id, f"Draft Team {number}")
        )
    fixtures.save_draft(season.season_id, [entry.season_entry_id for entry in entries])  # never frozen

    body = client.get(f"/api/public/seasons/{season.season_id}/rounds/1").json()
    assert body["round_state"] == "scheduled"
    assert body["matchups"] == []


def test_previous_and_next_navigation(season_client):
    client = season_client
    round_row, _ = _publish_round_one(client.app.state.database, 9004)
    season_id = round_row.season_id

    round_one = client.get(f"/api/public/seasons/{season_id}/rounds/1").json()
    assert round_one["prev_round_number"] is None
    assert round_one["next_round_number"] == 2

    round_two = client.get(f"/api/public/seasons/{season_id}/rounds/2").json()
    assert round_two["prev_round_number"] == 1
    assert round_two["next_round_number"] == 3

    last = client.get(f"/api/public/seasons/{season_id}/rounds/20").json()
    assert last["prev_round_number"] == 19
    assert last["next_round_number"] is None


def test_explicit_round_selection_by_number(season_client):
    client = season_client
    round_row, _ = _publish_round_one(client.app.state.database, 9005)
    season_id = round_row.season_id

    body = client.get(f"/api/public/seasons/{season_id}/rounds/13").json()
    assert body["round_number"] == 13
    assert body["label"] == "Round 13"
    assert body["round_state"] == "scheduled"


def test_round_number_out_of_range_returns_404(season_client):
    client = season_client
    round_row, _ = _publish_round_one(client.app.state.database, 9006)
    season_id = round_row.season_id

    assert client.get(f"/api/public/seasons/{season_id}/rounds/0").status_code == 404
    assert client.get(f"/api/public/seasons/{season_id}/rounds/21").status_code == 404
    assert client.get(f"/seasons/{season_id}/rounds/21").status_code == 404


def test_unknown_season_returns_404_everywhere(season_client):
    client = season_client
    assert client.get("/seasons/does-not-exist").status_code == 404
    assert client.get("/seasons/does-not-exist/rounds/1").status_code == 404
    assert client.get("/api/public/seasons/does-not-exist/rounds").status_code == 404
    assert client.get("/api/public/seasons/does-not-exist/rounds/1").status_code == 404
    assert client.get("/api/public/seasons/does-not-exist/rounds/1/ladder").status_code == 404


def test_future_round_shows_scheduled_preview_without_scores_or_lineups(season_client):
    client = season_client
    round_row, _ = _publish_round_one(client.app.state.database, 9007)
    season_id = round_row.season_id

    preview = client.get(f"/api/public/seasons/{season_id}/rounds/7").json()
    assert preview["round_state"] == "scheduled"
    assert preview["round_id"] is None
    assert preview["published"] is False
    assert len(preview["matchups"]) == 5
    for matchup in preview["matchups"]:
        assert matchup["status"] == "scheduled"
        assert set(matchup["home"].keys()) == {"team"}
        assert set(matchup["away"].keys()) == {"team"}
        assert matchup["home"]["team"]["name"]
        assert matchup["away"]["team"]["name"]
        assert "not authoritative" in matchup["status_label"]

    # The public contract is constructed by inclusion: no scorer/private
    # identifiers ever leak into a preview.
    encoded = json.dumps(preview).lower()
    for forbidden in ("season_entry_id", "coach", "licence", "mapping_id", "email", "token", "audit"):
        assert forbidden not in encoded


def test_published_round_shows_scores_winner_and_publication_status(season_client):
    client = season_client
    round_row, _ = _publish_round_one(client.app.state.database, 9008)
    season_id = round_row.season_id

    body = client.get(f"/api/public/seasons/{season_id}/rounds/1").json()
    assert body["round_state"] == "final"
    assert body["published"] is True
    assert body["round_id"] == round_row.bbbffl_round_id
    assert {m["status"] for m in body["matchups"]} == {"official"}
    for matchup in body["matchups"]:
        assert matchup["home"]["official_score"] is not None
        assert matchup["away"]["official_score"] is not None
        assert matchup["home"]["team"]["name"] and matchup["away"]["team"]["name"]
        # A published matchup's official publication instant is exposed as
        # a UTC ISO timestamp on the wire -- public templates render it in
        # Australian local time; see test_public_templates_use_australian_local_time.
        assert matchup["published_at"] is not None

    encoded = json.dumps(body).lower()
    for forbidden in ("season_entry_id", "lineup_id", "coach", "licence", "mapping_id", "email", "token"):
        assert forbidden not in encoded


def test_unpublished_round_never_contributes_to_the_ladder(season_client):
    client = season_client
    database = client.app.state.database
    lifecycle, round_one, _ = operational(database, 9009, afl_round=100)
    progress_to_review(lifecycle, round_one.bbbffl_round_id)  # open -> live -> review, never published
    season_id = lifecycle.get_round(round_one.bbbffl_round_id).season_id

    body = client.get(f"/api/public/seasons/{season_id}/rounds/1").json()
    assert body["round_state"] == "review"
    assert body["published"] is False
    assert {m["status"] for m in body["matchups"]} == {"upcoming"}

    ladder = client.get(f"/api/public/seasons/{season_id}/rounds/1/ladder").json()
    assert all(row["played"] == 0 for row in ladder["rows"])


def test_historical_ladder_reflects_only_results_published_through_the_selected_round(season_client):
    client = season_client
    round_row, _ = _publish_round_one(client.app.state.database, 9010)
    season_id = round_row.season_id

    # The ladder for a future, unplayed round must show exactly what was
    # published through Round 1 -- never "today's" ladder mislabelled, and
    # never inflated by rounds that have not been played yet.
    ladder_round_1 = client.get(f"/api/public/seasons/{season_id}/rounds/1/ladder").json()
    ladder_round_10 = client.get(f"/api/public/seasons/{season_id}/rounds/10/ladder").json()
    assert ladder_round_1["through_round"] == 1
    assert ladder_round_10["through_round"] == 10
    assert ladder_round_1["rows"] == ladder_round_10["rows"]
    assert all(row["played"] == 1 for row in ladder_round_1["rows"])
    assert all(row["team_name"] for row in ladder_round_1["rows"])
    assert all("season_entry_id" not in row for row in ladder_round_1["rows"])


def test_matchup_links_to_existing_detailed_view_only_once_a_round_is_opened(season_client):
    client = season_client
    round_row, _ = _publish_round_one(client.app.state.database, 9011)
    season_id = round_row.season_id

    opened = client.get(f"/api/public/seasons/{season_id}/rounds/1").json()
    assert opened["round_id"] == round_row.bbbffl_round_id
    detail = client.get(f"/seasons/{season_id}/rounds/{opened['round_id']}")
    assert detail.status_code == 200
    # Stable per-matchup anchors so a shared link can point at one matchup.
    assert 'id="match-' in detail.text

    preview = client.get(f"/api/public/seasons/{season_id}/rounds/9").json()
    assert preview["round_id"] is None  # nothing to link to yet


def test_season_round_browser_page_serves_a_canonical_url(season_client):
    client = season_client
    round_row, _ = _publish_round_one(client.app.state.database, 9012)
    season_id = round_row.season_id

    page = client.get(f"/seasons/{season_id}/rounds/1")
    assert page.status_code == 200
    assert 'name="viewport"' in page.text
    assert 'id="roundSelect"' in page.text
    assert 'id="prevBtn"' in page.text and 'id="nextBtn"' in page.text

    # The same URL is stable/shareable regardless of what is published --
    # a future round number renders the same page shell.
    future_page = client.get(f"/seasons/{season_id}/rounds/15")
    assert future_page.status_code == 200


def test_public_templates_use_australian_local_time_not_raw_utc(season_client):
    """Issue #161: public pages render Australian local time; UTC stays
    confined to the JSON API (for diagnostics), never shown directly."""
    browser_html = Path("app/templates/public_season_rounds.html").read_text()
    centre_html = Path("app/templates/public_round_centre.html").read_text()
    assert "Australia/Melbourne" in browser_html
    assert "Australia/Melbourne" in centre_html


def test_root_lands_on_a_season_with_rounds_defined_even_before_any_round_is_opened(season_client):
    client = season_client
    database = client.app.state.database
    logical_round, _ = configured(database, 9013)
    row = database.execute(
        "SELECT season_id FROM competition_stream WHERE competition_id=?", (logical_round.competition_id,)
    ).fetchone()
    season_id = row["season_id"]

    root = client.get("/", follow_redirects=False)
    assert root.status_code == 302
    assert root.headers["location"] == f"/seasons/{season_id}"

    overview = client.get(f"/seasons/{season_id}", follow_redirects=False)
    assert overview.status_code == 302
    assert overview.headers["location"] == f"/seasons/{season_id}/rounds/1"

    preview = client.get(f"/seasons/{season_id}/rounds/1")
    assert preview.status_code == 200
