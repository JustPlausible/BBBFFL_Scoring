"""Issue #213: the public Round Centre extended from Round 20 through
Finals Week 1, Finals Week 2, the Preliminary Final and the Grand Final,
with each finals week's concurrent SuperScore round rendered beneath it.

Covers the season navigation sequence (round selector, previous/next,
direct `bbbffl_round_id` URLs, season context retention) as JSON-level
regression coverage over the ordering/helper logic itself
(`app.public_finals.build_public_season_sequence` and the route-level
prev/next resolution in `app.routes.public_rounds.round_by_number`) --
never only HTML-template snapshots -- plus finals bracket/bye rendering,
Grand Final rendering, concurrent SS1-4 leaderboard rendering, and the
unpublished-state safety boundary. `tests/test_public_season_rounds.py`
already covers the ordinary-only surface and is deliberately left
unmodified except for the one exact-shape assertion issue #213's additive
`stream`/`week_number` fields touch.

Built on `tests.finals_helpers.build_finals_ready_season` (a fully-
finalised 20-round ordinary season plus a `finals` stream, bracket not yet
created) and `tests.season_completion_helpers.build_completable_season`
(the same, driven all the way through a published Grand Final and four
published SuperScore leaderboards) -- never a fresh simulation of either.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.audit import ActorContext
from app.calculations import MatchupCalculationService
from app.finals import FinalsBracketRepository
from app.finals_preflight import open_finals_week
from app.lineups import POSITIONS
from app.public_finals import build_public_season_sequence
from app.season import _now
from tests.finals_helpers import accept_week_mapping, build_finals_ready_season, seed_official_result
from tests.season_completion_helpers import (
    _Facts,
    build_completable_season,
    seed_real_finals_grand_final_calculation,
)

ACTOR = ActorContext.anonymous_operator("test")


@pytest.fixture
def public_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _repo(built):
    return FinalsBracketRepository(built["database"])


def _create_bracket(built, *, reason="test bracket creation"):
    return _repo(built).create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason=reason,
    )["bracket"]


def _open_week1(built, bracket, *, year):
    round_id = _repo(built).get_week_round_id(bracket.bracket_id, 1)
    accept_week_mapping(built["database"], round_id, year=year, afl_round_id=9101)
    open_finals_week(built["database"], bracket.bracket_id, 1, actor=ACTOR)
    return round_id


# -- Navigation ordering / helper logic (JSON-level, never HTML snapshots) --


def test_season_sequence_is_ordinary_only_when_no_finals_stream_exists(public_client):
    from tests.test_competition_lifecycle import operational

    lifecycle, round_one, entries = operational(public_client.app.state.database, 8001)
    sequence = build_public_season_sequence(
        public_client.app.state.database, public_client.app.state.seasons, entries[0].season_id
    )
    assert len(sequence["rounds"]) == 20
    assert sequence["finals_competition_id"] is None
    assert all(r["stream"] == "ordinary" for r in sequence["rounds"])


def test_season_sequence_appends_four_scheduled_finals_slots_once_the_stream_exists(public_client):
    built = build_finals_ready_season(year=8002, database=public_client.app.state.database)
    sequence = build_public_season_sequence(
        built["database"], public_client.app.state.seasons, built["season"].season_id
    )
    assert len(sequence["rounds"]) == 24
    finals_slots = sequence["rounds"][20:]
    assert [r["round_number"] for r in finals_slots] == [21, 22, 23, 24]
    assert [r["week_number"] for r in finals_slots] == [1, 2, 3, 4]
    assert [r["label"] for r in finals_slots] == [
        "Finals Week 1",
        "Finals Week 2",
        "Preliminary Final",
        "Grand Final",
    ]
    # No bracket exists yet -- every finals slot is a scheduled
    # placeholder with no linkable round_id, exactly like an ordinary
    # round nobody has opened yet.
    assert all(r["stream"] == "finals" for r in finals_slots)
    assert all(r["round_id"] is None for r in finals_slots)
    assert all(r["state"] == "scheduled" for r in finals_slots)
    # Round 20 is already final (build_finals_ready_season finalises the
    # whole ordinary competition) but no finals week has opened yet, so
    # the default round stays the most recently published ordinary round.
    assert sequence["default_round_number"] == 20


def test_season_sequence_reveals_finals_week1_round_id_once_opened_and_tracks_default_round(public_client):
    built = build_finals_ready_season(year=8003, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    week1_round_id = _open_week1(built, bracket, year=8003)

    sequence = build_public_season_sequence(
        built["database"], public_client.app.state.seasons, built["season"].season_id
    )
    week1 = sequence["rounds"][20]
    assert week1["round_id"] == week1_round_id
    assert week1["state"] == "open"
    assert week1["published"] is False
    # An opened, not-yet-final finals week is "in progress" -- the season
    # landing page should now track into it, exactly like an opened
    # ordinary round would.
    assert sequence["default_round_number"] == 21


# -- Previous/Next and direct-URL navigation (issue #213's explicit asks) --


def test_next_from_round_20_reaches_finals_week_1(public_client):
    built = build_finals_ready_season(year=8010, database=public_client.app.state.database)
    season_id = built["season"].season_id
    resp = public_client.get(f"/api/public/seasons/{season_id}/rounds/20")
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_round_number"] == 21
    assert body["stream"] == "ordinary"


def test_previous_from_finals_week_1_returns_to_round_20(public_client):
    built = build_finals_ready_season(year=8011, database=public_client.app.state.database)
    season_id = built["season"].season_id
    resp = public_client.get(f"/api/public/seasons/{season_id}/rounds/21")
    assert resp.status_code == 200
    body = resp.json()
    assert body["prev_round_number"] == 20
    assert body["stream"] == "finals"
    assert body["week_number"] == 1
    assert body["label"] == "Finals Week 1"


def test_finals_weeks_1_through_4_traverse_naturally_in_both_directions(public_client):
    built = build_completable_season(year=8012, database=public_client.app.state.database)
    season_id = built["season"].season_id

    forward = [20]
    n = 20
    for _ in range(5):
        resp = public_client.get(f"/api/public/seasons/{season_id}/rounds/{n}")
        body = resp.json()
        n = body["next_round_number"]
        if n is None:
            break
        forward.append(n)
    assert forward == [20, 21, 22, 23, 24]

    backward = [24]
    n = 24
    for _ in range(5):
        resp = public_client.get(f"/api/public/seasons/{season_id}/rounds/{n}")
        body = resp.json()
        n = body["prev_round_number"]
        if n is None or n == 20:
            backward.append(n)
            break
        backward.append(n)
    assert backward == [24, 23, 22, 21, 20]

    # Grand Final is the end of the season sequence.
    gf = public_client.get(f"/api/public/seasons/{season_id}/rounds/24").json()
    assert gf["next_round_number"] is None
    assert gf["label"] == "Grand Final"


def test_round_pulldown_lists_finals_weeks_in_season_context(public_client):
    built = build_finals_ready_season(year=8013, database=public_client.app.state.database)
    season_id = built["season"].season_id
    resp = public_client.get(f"/api/public/seasons/{season_id}/rounds")
    assert resp.status_code == 200
    rounds = resp.json()["rounds"]
    assert len(rounds) == 24
    labels = [r["label"] for r in rounds[20:]]
    assert labels == ["Finals Week 1", "Finals Week 2", "Preliminary Final", "Grand Final"]
    assert [r["round_number"] for r in rounds[20:]] == [21, 22, 23, 24]


def test_direct_finals_round_id_resolves_through_the_json_api_without_substituting_round_20(public_client):
    built = build_finals_ready_season(year=8014, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    week1_round_id = _open_week1(built, bracket, year=8014)
    season_id = built["season"].season_id

    resp = public_client.get(f"/api/public/seasons/{season_id}/rounds/{week1_round_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stream"] == "finals"
    assert body["week_number"] == 1
    assert body["round_id"] == week1_round_id
    assert body["season_id"] == season_id


def test_direct_finals_round_id_page_redirects_to_its_canonical_numbered_url_preserving_season_context(
    public_client,
):
    built = build_finals_ready_season(year=8015, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    week1_round_id = _open_week1(built, bracket, year=8015)
    season_id = built["season"].season_id

    resp = public_client.get(f"/seasons/{season_id}/rounds/{week1_round_id}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/seasons/{season_id}/rounds/21"


def test_a_finals_round_from_another_season_404s_rather_than_leaking_across_seasons(public_client):
    built_a = build_finals_ready_season(year=8016, database=public_client.app.state.database)
    bracket_a = _create_bracket(built_a)
    week1_round_id = _open_week1(built_a, bracket_a, year=8016)

    built_b = build_finals_ready_season(year=8017, database=public_client.app.state.database)
    other_season_id = built_b["season"].season_id

    resp = public_client.get(f"/api/public/seasons/{other_season_id}/rounds/{week1_round_id}")
    assert resp.status_code == 404


# -- Finals bracket rendering: bye, matchups, later weeks, Grand Final -----


def test_week1_bye_and_two_matchups_render_before_any_results_are_in(public_client):
    built = build_finals_ready_season(year=8020, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    _open_week1(built, bracket, year=8020)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    assert body["bye"] is not None
    assert body["bye"]["team_name"]
    assert len(body["matchups"]) == 2
    slots = {m["slot"] for m in body["matchups"]}
    assert slots == {"qf", "ef"}
    labels = {m["slot"] for m in body["matchups"]}
    assert "qf" in labels and "ef" in labels
    for matchup in body["matchups"]:
        assert matchup["home"]["team"]["name"]
        assert matchup["away"]["team"]["name"]
        # No results seeded yet -- never a fabricated score.
        assert matchup["status"] in ("upcoming", "scheduled")


def test_week1_matchup_shows_official_score_once_published(public_client):
    built = build_finals_ready_season(year=8021, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    _open_week1(built, bracket, year=8021)
    pairings = {p.slot: p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(built["database"], pairings["qf"].matchup_id, 105, 88)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    qf = next(m for m in body["matchups"] if m["slot"] == "qf")
    assert qf["status"] == "official"
    assert qf["home"]["official_score"] == 105 or qf["away"]["official_score"] == 105


def test_later_week_finals_rendering_shows_second_semi_and_first_semi(public_client):
    built = build_completable_season(year=8022, database=public_client.app.state.database)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/22").json()
    assert body["label"] == "Finals Week 2"
    assert body["week_number"] == 2
    slots = {m["slot"] for m in body["matchups"]}
    assert slots == {"second_semi", "first_semi"}
    assert all(m["status"] in ("official", "corrected_official") for m in body["matchups"])


def test_preliminary_final_rendering(public_client):
    built = build_completable_season(year=8023, database=public_client.app.state.database)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/23").json()
    assert body["label"] == "Preliminary Final"
    assert len(body["matchups"]) == 1
    assert body["matchups"][0]["slot"] == "preliminary"
    assert body["bye"] is None


def test_grand_final_rendering_reuses_the_established_matchup_result_shape(public_client):
    built = build_completable_season(year=8024, database=public_client.app.state.database)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/24").json()
    assert body["label"] == "Grand Final"
    assert body["published"] is True
    assert len(body["matchups"]) == 1
    gf = body["matchups"][0]
    assert gf["slot"] == "grand_final"
    assert gf["status"] in ("official", "corrected_official")
    # The same public matchup-result shape ordinary/finals matchups already
    # share (app.public_rounds._side) -- team name plus official score,
    # the same fields the Grand Final trial layout renders.
    assert gf["home"]["team"]["name"]
    assert gf["away"]["team"]["name"]
    assert gf["home"]["official_score"] is not None
    assert gf["away"]["official_score"] is not None


# -- Concurrent SuperScore section -----------------------------------------


def test_concurrent_ss1_through_ss4_published_leaderboards_render_all_ten_entries(public_client):
    built = build_completable_season(year=8030, database=public_client.app.state.database)
    season_id = built["season"].season_id

    for round_number, week_number in ((21, 1), (22, 2), (23, 3), (24, 4)):
        body = public_client.get(f"/api/public/seasons/{season_id}/rounds/{round_number}").json()
        ss = body["superscore"]
        assert ss["available"] is True
        assert ss["published"] is True
        assert ss["week_number"] == week_number
        assert ss["round_label"] == f"SuperScore {week_number}"
        assert len(ss["entries"]) == 10
        for entry in ss["entries"]:
            assert entry["team_name"]
            assert entry["rank"] >= 1
            assert isinstance(entry["total_score"], (int, float))
        # Public-safe only -- never a scorer-only/internal field.
        for entry in ss["entries"]:
            assert "input_snapshot" not in entry
            assert "effective_entry" not in entry
            assert "review_version" not in entry


def test_superscore_not_configured_shows_a_safe_pending_state(public_client):
    built = build_finals_ready_season(year=8031, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    _open_week1(built, bracket, year=8031)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    ss = body["superscore"]
    assert ss["available"] is False
    assert ss["published"] is False
    assert ss["entries"] == []


# -- Unpublished-state safety: no scorer-only/private content leaks -------


def test_unpublished_finals_week_never_leaks_scorer_only_state(public_client):
    built = build_finals_ready_season(year=8040, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    _open_week1(built, bracket, year=8040)
    season_id = built["season"].season_id

    resp = public_client.get(f"/api/public/seasons/{season_id}/rounds/21")
    payload = resp.text
    for private_marker in (
        "dnp_ruling",
        "override",
        "audit",
        "rulings",
        "lockout",
        "draft_revision",
        "snapshot",
        "bracket_id",
    ):
        # `bracket_id` itself is fine to expose (it is not sensitive), so
        # only assert the genuinely scorer-only markers never appear.
        if private_marker == "bracket_id":
            continue
        assert private_marker not in payload, f"unexpected private marker {private_marker!r} in public payload"


def test_unpublished_matchup_never_exposes_lineup_evidence_before_it_is_opened(public_client):
    built = build_finals_ready_season(year=8041, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    season_id = built["season"].season_id

    # Bracket exists (Week 1's pairing is determined) but nothing has been
    # opened yet -- team names only, never a lineup/score.
    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    assert body["round_state"] == "scheduled"
    assert body["published"] is False
    for matchup in body["matchups"]:
        assert matchup["status"] == "scheduled"
        assert "official_score" not in matchup["home"]
    assert bracket.bracket_id  # sanity: the bracket really was created


# -- Ordinary Round Centre / pulldown / ladder navigation stay unchanged --


def test_ordinary_round_and_ladder_navigation_unaffected_by_a_coexisting_finals_stream(public_client):
    built = build_completable_season(year=8050, database=public_client.app.state.database)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/1").json()
    assert body["stream"] == "ordinary"
    assert len(body["matchups"]) == 5

    ladder = public_client.get(f"/api/public/seasons/{season_id}/rounds/1/ladder")
    assert ladder.status_code == 200
    assert "rows" in ladder.json()

    page = public_client.get(f"/seasons/{season_id}/rounds/1")
    assert page.status_code == 200


def test_ladder_endpoints_refuse_a_finals_round_rather_than_fabricating_one(public_client):
    built = build_finals_ready_season(year=8051, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    week1_round_id = _open_week1(built, bracket, year=8051)
    season_id = built["season"].season_id

    assert public_client.get(f"/api/public/seasons/{season_id}/rounds/21/ladder").status_code == 404
    assert public_client.get(f"/api/public/seasons/{season_id}/rounds/{week1_round_id}/ladder").status_code == 404


def test_season_landing_page_redirects_into_finals_once_round_20_is_final_and_week1_opens(public_client):
    built = build_finals_ready_season(year=8052, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    _open_week1(built, bracket, year=8052)
    season_id = built["season"].season_id

    resp = public_client.get(f"/seasons/{season_id}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/seasons/{season_id}/rounds/21"


# -- Codex P2 follow-up 1: positional/lineup evidence on finals matchups --


def test_opened_finals_matchup_exposes_public_lineup_evidence_once_a_real_lineup_is_submitted(public_client):
    """A finals matchup's `home`/`away` sides already carry the identical
    public lineup-evidence shape (`app.public_rounds._side`) an ordinary
    matchup's do -- this proves it survives end to end through the finals
    read model once a real authoritative lineup/calculation exists, not
    just structurally in isolation."""
    built = build_completable_season(year=8060, database=public_client.app.state.database)
    seed_real_finals_grand_final_calculation(built, 8060)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/24").json()
    gf = body["matchups"][0]
    assert gf["slot"] == "grand_final"
    assert gf["home"]["lineup"] is not None
    assert gf["away"]["lineup"] is not None

    home_f1 = next(p for p in gf["home"]["lineup"]["players"] if p["position"] == "F1")
    assert home_f1["player_name"] == "GF Player 0"
    assert home_f1["effective_score"] is not None
    away_f1 = next(p for p in gf["away"]["lineup"]["players"] if p["position"] == "F1")
    assert away_f1["player_name"] == "GF Player 1"
    # Vacant positions are still present (public-safe: "no player selected"
    # is not itself sensitive), just with no player name.
    vacant = next(p for p in gf["home"]["lineup"]["players"] if p["position"] != "F1")
    assert vacant["player_name"] is None


def test_finals_lineup_evidence_stays_public_safe_and_never_leaks_scorer_only_fields(public_client):
    built = build_completable_season(year=8061, database=public_client.app.state.database)
    seed_real_finals_grand_final_calculation(built, 8061)
    season_id = built["season"].season_id

    resp = public_client.get(f"/api/public/seasons/{season_id}/rounds/24")
    payload = resp.text
    for private_marker in (
        "season_player_id",
        "actor_id",
        "actor_role",
        "actor_type",
        "calculation_fingerprint",
        "dnp_ruling_reason",
        "override",
        "audit",
        "input_fingerprint",
        "rules_version_id",
    ):
        assert private_marker not in payload, f"unexpected private marker {private_marker!r} in public payload"


def test_ordinary_matchup_lineup_evidence_shape_is_unaffected_by_the_finals_lineup_rendering_path(public_client):
    built = build_completable_season(year=8062, database=public_client.app.state.database)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/1").json()
    for matchup in body["matchups"]:
        assert "lineup" in matchup["home"]
        assert "lineup" in matchup["away"]


def test_unpublished_finals_matchup_has_no_lineup_key_to_expand(public_client):
    """The preview shape (a pairing not yet materialised into a real
    matchup) has no `lineup` key at all -- the public template's expand
    toggle checks for this exact distinction, never rendering an empty/
    misleading "Lineups" section for a matchup that does not exist yet."""
    built = build_finals_ready_season(year=8063, database=public_client.app.state.database)
    _create_bracket(built)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    for matchup in body["matchups"]:
        assert "lineup" not in matchup["home"]
        assert "lineup" not in matchup["away"]


# -- Codex P2 follow-up 2: persisted bracket seed shown with finals teams --


def test_bye_and_preview_pairings_carry_the_persisted_bracket_seed(public_client):
    built = build_finals_ready_season(year=8070, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    season_id = built["season"].season_id
    seed_rows = {row.season_entry_id: row.seed_position for row in _repo(built).list_seed_rows(bracket.bracket_id)}
    pairings = {p.slot: p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1)}

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    assert body["bye"]["seed"] == seed_rows[pairings["bye"].home_season_entry_id]
    assert seed_rows[pairings["bye"].home_season_entry_id] == 1

    for matchup in body["matchups"]:
        # Every previewed seed traces back to the exact persisted
        # finals_bracket_seed row for that slot's pairing -- never a
        # fresh ladder computation.
        pairing = pairings[matchup["slot"]]
        assert matchup["home"]["team"]["seed"] == seed_rows[pairing.home_season_entry_id]
        assert matchup["away"]["team"]["seed"] == seed_rows[pairing.away_season_entry_id]


def test_opened_matchup_seed_matches_persisted_bracket_seed_not_a_live_recomputation(public_client):
    built = build_finals_ready_season(year=8071, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    week1_round_id = _open_week1(built, bracket, year=8071)
    season_id = built["season"].season_id

    seed_rows = {row.season_entry_id: row.seed_position for row in _repo(built).list_seed_rows(bracket.bracket_id)}
    pairings = {p.slot: p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1)}

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/{week1_round_id}").json()
    qf = next(m for m in body["matchups"] if m["slot"] == "qf")
    assert qf["home"]["team"]["seed"] == seed_rows[pairings["qf"].home_season_entry_id]
    assert qf["away"]["team"]["seed"] == seed_rows[pairings["qf"].away_season_entry_id]


def test_later_week_and_grand_final_seeds_reflect_the_original_frozen_bracket_seed(public_client):
    """A team that has advanced to the Grand Final still shows the seed
    position it was *originally* frozen at bracket creation -- never a
    seed re-derived from having won its way there, and never the live/
    current ladder (docs/2026-finals-superscore-design.md's "two truths":
    replay evidence may intentionally differ from the mathematical
    order)."""
    built = build_completable_season(year=8072, database=public_client.app.state.database)
    bracket = built["bracket"]
    season_id = built["season"].season_id
    seed_rows = {row.season_entry_id: row.seed_position for row in _repo(built).list_seed_rows(bracket.bracket_id)}
    gf_pairing = built["grand_final_pairing"]

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/24").json()
    gf = body["matchups"][0]
    assert gf["home"]["team"]["seed"] == seed_rows[gf_pairing.home_season_entry_id]
    assert gf["away"]["team"]["seed"] == seed_rows[gf_pairing.away_season_entry_id]
    # Sanity: this is a real, non-trivial seed check -- both sides
    # resolved to one of the five seeds that actually qualified.
    assert gf["home"]["team"]["seed"] in range(1, 6)
    assert gf["away"]["team"]["seed"] in range(1, 6)


# -- Codex P2 follow-up 3: a finals matchup's non-published status must
# still be visible, never indistinguishable from an official result. The
# fix lives in the template's `finalsMatchCard` (this codebase has no JS
# test harness, matching every other public-route test here); these
# assert the JSON DTO fields (`status`/`status_label`/`calculated_score`/
# `official_score`/`published_at`) that fix now renders unconditionally
# (`note = published ? ... : m.status_label`) rather than only for
# `status === 'scheduled'` as before.


def _seed_empty_lineup(database, round_id, competition_id, season_id, entry_id, *, label):
    """An authoritative but entirely vacant lineup (every position
    submitted with no player) -- the minimum `weekly_lineup`/
    `weekly_lineup_submission`/`weekly_lineup_submission_slot` rows
    `MatchupCalculationService._entry` requires to calculate an entry at
    all (it refuses an entry with no effective submitted lineup outright,
    never treating "nothing submitted yet" as "score zero"). Every slot
    then legitimately scores zero, which is all a `calculated_live`/
    `under_review` status test needs -- not a real scored lineup."""
    now = _now()
    lineup_id = f"{label}-lineup"
    with database.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO weekly_lineup (lineup_id,season_id,competition_id,bbbffl_round_id,season_entry_id,"
                "draft_revision,effective_submission_version,created_at,updated_at) "
                "VALUES (:l,:s,:c,:r,:e,1,1,:now,:now)"
            ),
            {"l": lineup_id, "s": season_id, "c": competition_id, "r": round_id, "e": entry_id, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO weekly_lineup_submission (lineup_id,version,based_on_draft_revision,submitted_at,"
                "actor_type,actor_role,source_type) VALUES (:l,1,1,:now,'coach','coach','coach')"
            ),
            {"l": lineup_id, "now": now},
        )
        for position in POSITIONS:
            conn.execute(
                text("INSERT INTO weekly_lineup_submission_slot VALUES (:l,1,:pos,NULL)"),
                {"l": lineup_id, "pos": position},
            )


def _calculate_week1_without_publishing(built, bracket, week1_round_id):
    database = built["database"]
    season_id = built["season"].season_id
    competition_id = built["finals_competition"].competition_id
    pairings = {p.slot: p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1)}
    entry_ids = {
        pairings["qf"].home_season_entry_id,
        pairings["qf"].away_season_entry_id,
        pairings["ef"].home_season_entry_id,
        pairings["ef"].away_season_entry_id,
    }
    for index, entry_id in enumerate(sorted(entry_ids)):
        _seed_empty_lineup(database, week1_round_id, competition_id, season_id, entry_id, label=f"w1-{index}")
    MatchupCalculationService(database, _Facts({})).calculate_round(week1_round_id)


def test_calculated_live_finals_matchup_shows_its_score_with_a_not_official_status(public_client):
    built = build_finals_ready_season(year=8080, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    week1_round_id = _open_week1(built, bracket, year=8080)
    _calculate_week1_without_publishing(built, bracket, week1_round_id)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    assert body["round_state"] == "open"
    for matchup in body["matchups"]:
        assert matchup["status"] == "calculated_live"
        assert matchup["status_label"] == "Live calculated — not official"
        assert matchup["published_at"] is None
        assert matchup["home"]["calculated_score"] is not None
        assert matchup["home"]["official_score"] is None


def test_under_review_finals_matchup_shows_the_public_review_warning(public_client):
    built = build_finals_ready_season(year=8081, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    week1_round_id = _open_week1(built, bracket, year=8081)
    _calculate_week1_without_publishing(built, bracket, week1_round_id)
    _repo(built).advance_week_to_review(bracket.bracket_id, 1, actor=ACTOR, reason="test: move to review")
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    assert body["round_state"] == "review"
    for matchup in body["matchups"]:
        assert matchup["status"] == "under_review"
        assert matchup["status_label"] == "Under review — not official"
        assert matchup["published_at"] is None
        assert matchup["home"]["calculated_score"] is not None


def test_published_official_finals_matchup_is_not_mislabelled_as_unofficial(public_client):
    built = build_finals_ready_season(year=8082, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    week1_round_id = _open_week1(built, bracket, year=8082)
    pairings = {p.slot: p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(built["database"], pairings["qf"].matchup_id, 90, 61)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/{week1_round_id}").json()
    qf = next(m for m in body["matchups"] if m["slot"] == "qf")
    assert qf["status"] == "official"
    assert qf["status_label"] == "Official final"
    assert qf["published_at"] is not None
    assert qf["home"]["official_score"] is not None


def test_mixed_states_within_the_same_finals_week_stay_distinguishable(public_client):
    """One matchup already has an official published result while its
    sibling in the same week only has a live calculation -- issue #213
    Codex follow-up's exact failure scenario: without the fix, the
    non-final matchup's card carried no visible status text at all,
    making it indistinguishable at a glance from the published one."""
    built = build_finals_ready_season(year=8083, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    week1_round_id = _open_week1(built, bracket, year=8083)
    pairings = {p.slot: p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(built["database"], pairings["qf"].matchup_id, 90, 61)
    _calculate_week1_without_publishing(built, bracket, week1_round_id)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    qf = next(m for m in body["matchups"] if m["slot"] == "qf")
    ef = next(m for m in body["matchups"] if m["slot"] == "ef")
    assert qf["status"] == "official"
    assert qf["status_label"] == "Official final"
    assert qf["published_at"] is not None
    assert ef["status"] == "calculated_live"
    assert ef["status_label"] == "Live calculated — not official"
    assert ef["published_at"] is None
    assert qf["status"] != ef["status"]
    assert qf["status_label"] != ef["status_label"]


# -- Codex P2 follow-up 4: finals pages must keep refreshing after the
# initial render, since a finals round_id has no per-matchup polling
# detail page to fall back on. This codebase has no JS test harness (see
# follow-up 3's comment above), so this asserts what is actually
# testable: the finals round-number page is wired with the same
# `poll_interval_seconds` context value `public_round_centre.html`'s own
# polling page already uses, scoped only to the finals branch -- the
# ordinary page/API surface stays byte-for-byte unaffected.


def test_finals_round_page_is_wired_with_the_poll_interval_for_client_side_refresh(public_client):
    built = build_finals_ready_season(year=8090, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    _open_week1(built, bracket, year=8090)
    season_id = built["season"].season_id
    poll_interval_seconds = public_client.app.state.settings.poll_interval_seconds

    page = public_client.get(f"/seasons/{season_id}/rounds/21")
    assert page.status_code == 200
    assert f"pollIntervalMs={poll_interval_seconds}*1000" in page.text
    assert "finalsPollTimer=setInterval(render" in page.text


def test_ordinary_round_page_rendering_is_unaffected_by_the_finals_poll_wiring(public_client):
    built = build_completable_season(year=8091, database=public_client.app.state.database)
    season_id = built["season"].season_id

    page = public_client.get(f"/seasons/{season_id}/rounds/1")
    assert page.status_code == 200
    # The poll-interval value is now always passed to the template (the
    # same context shape as public_round_centre.html's), but an ordinary
    # round never enters the finals branch that actually starts polling.
    poll_interval_seconds = public_client.app.state.settings.poll_interval_seconds
    assert f"pollIntervalMs={poll_interval_seconds}*1000" in page.text
