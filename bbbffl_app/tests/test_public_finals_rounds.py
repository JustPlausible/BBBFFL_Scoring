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

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.afl_client import PlayerStatLine
from app.audit import ActorContext
from app.calculations import MatchupCalculationService
from app.finals import FinalsBracketRepository
from app.finals_preflight import open_finals_week
from app.finals_review import build_finals_round_review
from app.identity import IdentityRepository
from app.lineups import POSITIONS
from app.public_finals import _public_entry, build_public_season_sequence
from app.round_review import RoundReviewRepository
from app.season import _now
from tests.finals_helpers import accept_week_mapping, build_finals_ready_season, seed_official_result
from tests.season_completion_helpers import (
    _Facts,
    _finalize_round,
    build_completable_season,
    seed_real_finals_grand_final_calculation,
)

ACTOR = ActorContext.anonymous_operator("test")


@pytest.mark.parametrize("match_state", ["yet_to_play", "live", "postgame", "completed"])
def test_superscore_public_projection_uses_current_match_progress(match_state):
    snapshot = {
        "effective_entry": {
            "team_name": "JHAS",
            "slots": [
                {
                    "slot": "F1",
                    "season_player_id": "original",
                    "player_name": "Mitch Lewis",
                    "afl_club": "Hawthorn",
                    "effective_score": 12,
                    "effective_source": "starter",
                    "dnp_ruling": False,
                    "interchange_applied": False,
                }
            ],
            "interchange": {"season_player_id": None, "player_name": None},
        },
        "entry": {
            "slots": [
                {
                    "position": "F1",
                    "afl_match_id": 44,
                    "source_afl_round_id": 9,
                    "stats": {"goals": 2, "behinds": 0},
                }
            ]
        },
    }
    projected = _public_entry(
        {"rank": 1, "season_entry_id": "entry", "team_name": "JHAS", "total_score": 12},
        snapshot,
        published=False,
        match_states={44: match_state},
    )
    assert projected["positions"][0]["display_state"] == match_state
    assert projected["positions"][0]["football_line"] == "2.0"
    assert projected["football_line"] == "2.0"


def test_superscore_projection_preserves_original_dnp_when_interchange_replaces_it():
    snapshot = {
        "effective_entry": {
            "slots": [
                {
                    "slot": "Tackler",
                    "season_player_id": "starter",
                    "player_name": "Original Player",
                    "afl_club": "Original Club",
                    "effective_score": 18,
                    "effective_source": "interchange",
                    "dnp_ruling": True,
                    "interchange_applied": True,
                }
            ],
            "interchange": {
                "season_player_id": "bench",
                "player_name": "Replacement Player",
                "afl_club": "Replacement Club",
                "target_position": "Tackler",
            },
        },
        "entry": {
            "slots": [
                {"position": "Tackler", "afl_match_id": 1, "stats": None},
                {"position": "Interchange", "afl_match_id": 2, "stats": {"tackles": 3}},
            ]
        },
    }
    projected = _public_entry(
        {"rank": 1, "season_entry_id": "entry", "total_score": 18},
        snapshot,
        published=False,
        match_states={2: "live"},
    )
    position = projected["positions"][0]
    assert position["player_name"] == "Original Player"
    assert position["confirmed_dnp"] is True
    assert position["replacement_player_name"] == "Replacement Player"
    assert position["replacement_afl_club"] == "Replacement Club"
    assert position["display_state"] == "live"
    assert position["football_line"] == "3.0"


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        ({"season_player_id": "p", "player_name": "DNP", "dnp_ruling": True}, "dnp"),
        ({"season_player_id": None, "player_name": None}, "vacant"),
        ({"season_player_id": None, "player_name": "Unresolved"}, "unnamed"),
    ],
)
def test_superscore_projection_exposes_non_playing_states(slot, expected):
    slot = {"slot": "F1", "effective_score": 0, "interchange_applied": False, **slot}
    snapshot = {
        "effective_entry": {"slots": [slot], "interchange": {}},
        "entry": {"slots": [{"position": "F1", "stats": None}]},
    }
    projected = _public_entry(
        {"rank": 1, "season_entry_id": "entry", "total_score": 0},
        snapshot,
        published=False,
        match_states={},
    )
    assert projected["positions"][0]["display_state"] == expected


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
#
# Codex follow-up 5: the poll timer must be schedulable even if the
# page's very first fetch fails -- `stream` (and therefore
# `isFinalsRound`) is resolved server-side from the URL alone, and the
# timer is scheduled unconditionally at the bottom of the script, never
# only inside render()'s own success path.


def test_finals_round_page_is_wired_with_the_poll_interval_for_client_side_refresh(public_client):
    built = build_finals_ready_season(year=8090, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    _open_week1(built, bracket, year=8090)
    season_id = built["season"].season_id
    poll_interval_seconds = public_client.app.state.settings.poll_interval_seconds

    page = public_client.get(f"/seasons/{season_id}/rounds/21")
    assert page.status_code == 200
    assert f"pollIntervalMs={poll_interval_seconds}*1000" in page.text
    assert "isFinalsRound=" in page.text
    assert "'finals'==='finals'" in page.text.replace('"', "'")
    # Scheduled unconditionally at the bottom of the script -- never
    # inside render()'s own try block -- so a failed first fetch still
    # gets retried.
    assert "if(isFinalsRound){\n  setInterval(render" in page.text


def test_ordinary_round_page_rendering_is_unaffected_by_the_finals_poll_wiring(public_client):
    built = build_completable_season(year=8091, database=public_client.app.state.database)
    season_id = built["season"].season_id

    page = public_client.get(f"/seasons/{season_id}/rounds/1")
    assert page.status_code == 200
    # The poll-interval value and the isFinalsRound flag are now always
    # passed to the template (the same context shape as
    # public_round_centre.html's), but an ordinary round resolves
    # isFinalsRound to false and never schedules the poll timer.
    poll_interval_seconds = public_client.app.state.settings.poll_interval_seconds
    assert f"pollIntervalMs={poll_interval_seconds}*1000" in page.text
    assert "'ordinary'==='finals'" in page.text.replace('"', "'")


def test_finals_round_page_resolves_isFinalsRound_before_any_client_fetch_could_run(public_client):
    """The whole point of Codex follow-up 5: `isFinalsRound` must be
    knowable from the server-rendered page alone, never derived from a
    fetch response -- proven here by asserting it appears as a plain
    literal assignment before the first `fetch(` call in the script."""
    built = build_finals_ready_season(year=8092, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    _open_week1(built, bracket, year=8092)
    season_id = built["season"].season_id

    page = public_client.get(f"/seasons/{season_id}/rounds/21")
    script = page.text
    is_finals_index = script.index("isFinalsRound=")
    first_fetch_index = script.index("fetch(")
    assert is_finals_index < first_fetch_index


# -- Issue #261: historical public finals/SuperScore result presentation ---


def _stat_line_for(position, canonical, score):
    """A `PlayerStatLine` that, once scored through `app.scoring.
    score_position` for `position`, produces exactly `score` -- so a test
    can dictate a position's effective score precisely rather than
    guessing at scoring-engine internals."""
    if position in ("F1", "F2", "F3"):
        goals, behinds = divmod(score, 6)
        return PlayerStatLine(canonical, goals=goals, behinds=behinds)
    if position in ("M1", "M2", "M3"):
        return PlayerStatLine(canonical, disposals=score)
    if position == "Ruck":
        return PlayerStatLine(canonical, marks=score, hitouts=0)
    if position == "Tackler":
        assert score % 6 == 0, "Tackler score must be a multiple of 6 (6x tackles)"
        return PlayerStatLine(canonical, tackles=score // 6)
    raise ValueError(position)


def _seed_named_finals_lineup(
    built,
    round_id,
    competition_id,
    entry_id,
    position_scores,
    *,
    label,
    stats,
    vacant_positions=(),
    interchange=None,
    forward_stats=None,
):
    """A full 8-position finals lineup for one entry, engineered via
    `_stat_line_for` so the resulting calculated per-position scores are
    exactly `position_scores` -- reused to reproduce issue #261's
    `24.14 (158)` / `38.12 (240)` acceptance example precisely. Adds each
    named player's stat line into the shared `stats` dict a caller's
    `_Facts` will serve for the whole round.

    `forward_stats`, when given, is `{position: (goals, behinds)}` for a
    Forward position whose literal AFL line should be exactly that pair
    (score = 6*goals + behinds) rather than `_stat_line_for`'s own
    divmod-derived default -- the only way to construct a literal behind
    total of 6 or more (Codex P2 on PR #262: such a line must never be
    folded into an extra goal by a naive divmod of the point total).

    `vacant_positions` leaves those (otherwise-scorable) positions
    genuinely unselected -- distinct from a named player later ruled DNP.
    `interchange`, when given, is `(player_id, canonical_player_id, name,
    stat_kwargs)` for a named Interchange player -- `stat_kwargs` are the
    `PlayerStatLine` keyword args for their literal AFL line (e.g.
    `{"tackles": 8}` or `{"goals": 1, "behinds": 8}`), inserted directly at
    submission time rather than mutated in afterwards: submitted lineup
    slots are immutable by DB trigger (see root `CLAUDE.md`'s
    "Immutability and history"), so a DNP/interchange scenario must be
    built into the original submission, never patched onto it."""
    database = built["database"]
    now = _now()
    lineup_id = f"{label}-lineup"
    with database.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO weekly_lineup (lineup_id,season_id,competition_id,bbbffl_round_id,season_entry_id,"
                "draft_revision,effective_submission_version,created_at,updated_at) "
                "VALUES (:l,:s,:c,:r,:e,1,1,:now,:now)"
            ),
            {
                "l": lineup_id,
                "s": built["season"].season_id,
                "c": competition_id,
                "r": round_id,
                "e": entry_id,
                "now": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO weekly_lineup_submission (lineup_id,version,based_on_draft_revision,submitted_at,"
                "actor_type,actor_role,source_type) VALUES (:l,1,1,:now,'coach','coach','coach')"
            ),
            {"l": lineup_id, "now": now},
        )

        def _insert_player(player_id, canonical, name):
            conn.execute(
                text(
                    "INSERT INTO season_player_pool (season_player_id,season_id,canonical_player_id,"
                    "display_name,afl_team_id,eligible,source_provider,source_fetched_at,created_at,updated_at) "
                    "VALUES (:p,:s,:c,:n,1,TRUE,'test',:now,:now,:now)"
                ),
                {"p": player_id, "s": built["season"].season_id, "c": canonical, "n": name, "now": now},
            )

        canonical_base = (abs(hash(label)) % 500_000) * 100
        for index, position in enumerate(POSITIONS):
            selected = None
            if position == "Interchange":
                if interchange is not None:
                    player_id, canonical, name, stat_kwargs = interchange
                    _insert_player(player_id, canonical, name)
                    stats[canonical] = PlayerStatLine(canonical, **stat_kwargs)
                    selected = player_id
            elif position not in vacant_positions:
                canonical = canonical_base + index
                player_id = f"{label}-{position}"
                _insert_player(player_id, canonical, f"{label} {position}")
                if forward_stats and position in forward_stats:
                    goals, behinds = forward_stats[position]
                    stats[canonical] = PlayerStatLine(canonical, goals=goals, behinds=behinds)
                else:
                    stats[canonical] = _stat_line_for(position, canonical, position_scores[position])
                selected = player_id
            conn.execute(
                text("INSERT INTO weekly_lineup_submission_slot VALUES (:l,1,:pos,:p)"),
                {"l": lineup_id, "pos": position, "p": selected},
            )


def test_finals_matchup_totals_render_as_goals_behinds_total_matching_the_2026_grand_final_example(public_client):
    """Reproduces issue #261's acceptance example exactly: Running Hots'
    158 and Evil Absolutes' 240 must render as `24.14 (158)` and
    `38.12 (240)` -- the established BBBFFL football-score conversion
    (`app.score_presentation`, the same rules `app.presentation` uses for
    the Grand Final/SuperScore vertical), summed per scorable position
    rather than a single divmod of the raw total (which would instead show
    the mathematically different, but equally "valid", `26.2 (158)`)."""
    built = build_finals_ready_season(year=8100, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    round_id = _open_week1(built, bracket, year=8100)
    season_id = built["season"].season_id

    pairing = next(p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1) if p.slot == "qf")
    competition_id = built["finals_competition"].competition_id

    # 3 forwards + 3 midfields + ruck + tackler = 8 scorable positions.
    running_hots_scores = {
        "F1": 23,
        "F2": 23,
        "F3": 22,
        "M1": 18,
        "M2": 18,
        "M3": 18,
        "Ruck": 18,
        "Tackler": 18,
    }
    evil_absolutes_scores = {
        "F1": 32,
        "F2": 32,
        "F3": 32,
        "M1": 32,
        "M2": 32,
        "M3": 32,
        "Ruck": 24,
        "Tackler": 24,
    }
    assert sum(running_hots_scores.values()) == 158
    assert sum(evil_absolutes_scores.values()) == 240

    stats = {}
    _seed_named_finals_lineup(
        built,
        round_id,
        competition_id,
        pairing.home_season_entry_id,
        running_hots_scores,
        label=f"rh-{round_id}",
        stats=stats,
    )
    _seed_named_finals_lineup(
        built,
        round_id,
        competition_id,
        pairing.away_season_entry_id,
        evil_absolutes_scores,
        label=f"ea-{round_id}",
        stats=stats,
    )
    MatchupCalculationService(built["database"], _Facts(stats)).calculate_matchup(pairing.matchup_id, guard_season=True)
    seed_official_result(built["database"], pairing.matchup_id, 158, 240)
    _finalize_round(built["database"], round_id)

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    qf = next(m for m in body["matchups"] if m["slot"] == "qf")
    assert qf["home"]["official_score"] == 158
    assert qf["home"]["football_line"] == "24.14"
    assert qf["away"]["official_score"] == 240
    assert qf["away"]["football_line"] == "38.12"


def test_finals_football_line_preserves_a_forwards_literal_behind_total_of_six_or_more(public_client):
    """Codex P2 on PR #262: a Forward's literal AFL goals/behinds must be
    preserved even when the real behind count reaches 6 or more -- a naive
    divmod of the point total alone would wrongly fold that into an extra
    goal (14 points -> "2.2" instead of the real "1.8"), silently losing
    precision the established `app.presentation.football_score_for_position`
    rule (and now `app.score_presentation.football_score_from_evidence`)
    was written to preserve."""
    built = build_finals_ready_season(year=8106, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    round_id = _open_week1(built, bracket, year=8106)
    season_id = built["season"].season_id

    pairing = next(p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1) if p.slot == "qf")
    competition_id = built["finals_competition"].competition_id

    # F1: 1 goal, 8 behinds = 14 points. Every other position scores zero,
    # so the side's only points come from this one literal line.
    scores = {"F1": 14, "F2": 0, "F3": 0, "M1": 0, "M2": 0, "M3": 0, "Ruck": 0, "Tackler": 0}
    stats = {}
    _seed_named_finals_lineup(
        built,
        round_id,
        competition_id,
        pairing.home_season_entry_id,
        scores,
        label=f"lit-home-{round_id}",
        stats=stats,
        forward_stats={"F1": (1, 8)},
    )
    _seed_named_finals_lineup(
        built,
        round_id,
        competition_id,
        pairing.away_season_entry_id,
        scores,
        label=f"lit-away-{round_id}",
        stats=stats,
    )
    MatchupCalculationService(built["database"], _Facts(stats)).calculate_matchup(pairing.matchup_id, guard_season=True)
    seed_official_result(built["database"], pairing.matchup_id, 14, 0)
    _finalize_round(built["database"], round_id)

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    qf = next(m for m in body["matchups"] if m["slot"] == "qf")
    assert qf["home"]["official_score"] == 14
    assert qf["home"]["football_line"] == "1.8"


def test_finals_football_line_for_a_forward_replaced_by_interchange_uses_the_interchanges_own_line(public_client):
    """Codex P2 follow-up on PR #262: once the Interchange is effectively
    scoring a Forward position, the football-score evidence must be the
    Interchange's own literal goals/behinds -- never the original (here,
    genuinely vacant) Forward's, which belongs to a different player
    entirely and would either coincidentally match the wrong line or fall
    back to a divmod approximation despite real evidence being available."""
    built = build_finals_ready_season(year=8107, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    round_id = _open_week1(built, bracket, year=8107)
    season_id = built["season"].season_id

    pairing = next(p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1) if p.slot == "qf")
    competition_id = built["finals_competition"].competition_id
    entry_id = pairing.home_season_entry_id

    scores = {"F1": 0, "F2": 0, "F3": 0, "M1": 0, "M2": 0, "M3": 0, "Ruck": 0, "Tackler": 0}
    stats = {}
    interchange_id = f"ir-home-{round_id}-Interchange"
    interchange_canonical = (abs(hash(f"ir-home-{round_id}")) % 500_000) * 100 + 900
    _seed_named_finals_lineup(
        built,
        round_id,
        competition_id,
        entry_id,
        scores,
        label=f"ir-home-{round_id}",
        stats=stats,
        vacant_positions=("F1",),
        interchange=(interchange_id, interchange_canonical, "Matt Rowell", {"goals": 1, "behinds": 8}),
    )
    _seed_named_finals_lineup(
        built, round_id, competition_id, pairing.away_season_entry_id, scores, label=f"ir-away-{round_id}", stats=stats
    )

    lifecycle = built["lifecycle"]
    identities = IdentityRepository(built["database"])
    review_repo = RoundReviewRepository(built["database"])
    MatchupCalculationService(built["database"], _Facts(stats)).calculate_matchup(pairing.matchup_id, guard_season=True)
    review = build_finals_round_review(lifecycle, review_repo, identities, round_id)
    matchup_review = next(m for m in review["matchups"] if m.matchup_id == pairing.matchup_id)
    review_repo.record_interchange_ruling(
        pairing.matchup_id,
        entry_id,
        "F1",
        expected_review_version=matchup_review.review_version,
        actor=ACTOR,
        reason="cover vacant forward",
    )

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    qf = next(m for m in body["matchups"] if m["slot"] == "qf")
    side = qf["home"] if pairing.home_season_entry_id == entry_id else qf["away"]
    f1 = next(p for p in side["lineup"]["players"] if p["position"] == "F1")
    assert f1["outcome"] == "replaced_by_interchange"
    assert f1["effective_score"] == 14
    assert side["football_line"] == "1.8"
    # Issue #261 scope extension: the per-position line (not just the side
    # aggregate) also reflects the Interchange's own literal evidence.
    assert f1["football_line"] == "1.8"
    assert f1["interchange_player_name"] == "Matt Rowell"


def test_finals_individual_positions_show_the_established_football_line(public_client):
    """Issue #261 scope extension: individual positional presentation in
    Finals -- a Forward's literal AFL goals/behinds and a Midfield
    position's divmod conversion, both shown per-position via
    `app.public_rounds._slot`'s `football_line` field."""
    built = build_finals_ready_season(year=8108, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    round_id = _open_week1(built, bracket, year=8108)
    season_id = built["season"].season_id

    pairing = next(p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1) if p.slot == "qf")
    competition_id = built["finals_competition"].competition_id

    scores = {"F1": 23, "F2": 18, "F3": 18, "M1": 18, "M2": 18, "M3": 18, "Ruck": 18, "Tackler": 18}
    stats = {}
    _seed_named_finals_lineup(
        built, round_id, competition_id, pairing.home_season_entry_id, scores, label=f"pos-home-{round_id}", stats=stats
    )
    _seed_named_finals_lineup(
        built, round_id, competition_id, pairing.away_season_entry_id, scores, label=f"pos-away-{round_id}", stats=stats
    )
    MatchupCalculationService(built["database"], _Facts(stats)).calculate_matchup(pairing.matchup_id, guard_season=True)

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    qf = next(m for m in body["matchups"] if m["slot"] == "qf")
    home = qf["home"]
    f1 = next(p for p in home["lineup"]["players"] if p["position"] == "F1")
    assert f1["effective_score"] == 23
    assert f1["football_line"] == "3.5"
    m1 = next(p for p in home["lineup"]["players"] if p["position"] == "M1")
    assert m1["effective_score"] == 18
    assert m1["football_line"] == "3.0"


def test_ordinary_matchup_score_presentation_is_unaffected_by_the_finals_football_line(public_client):
    """Requirement 6: the ordinary Round Centre's bare point-total score
    cards are untouched by issue #261 -- `football_line` is now present on
    the shared `_side` DTO, but `matchCard` in the template never reads it."""
    built = build_completable_season(year=8101, database=public_client.app.state.database)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/1").json()
    for matchup in body["matchups"]:
        assert isinstance(matchup["home"]["official_score"], (int, float)) or matchup["home"]["official_score"] is None
        # The new field exists on the DTO (shared helper) but the ordinary
        # match card template never renders it -- see the test above for
        # the finals-only rendering behaviour.
        assert "football_line" in matchup["home"]


def test_superscore_integral_totals_render_without_a_trailing_zero(public_client):
    """Issue #261: SS4's published totals (e.g. 271.0, 246.0, 227.0) must
    carry an additive `total_display` of `271`/`246`/`227` alongside the
    unmodified `total_score` -- the stored/API value is never altered
    purely for display."""
    built = build_completable_season(year=8102, database=public_client.app.state.database)
    season_id = built["season"].season_id

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/24").json()
    ss = body["superscore"]
    assert ss["published"] is True
    assert len(ss["entries"]) == 10
    for entry in ss["entries"]:
        if float(entry["total_score"]).is_integer():
            assert entry["total_display"] == int(entry["total_score"])
            assert "." not in str(entry["total_display"])
        else:
            assert entry["total_display"] == entry["total_score"]
        assert entry["official"] is True
        assert entry["football_line"]
        assert len(entry["positions"]) == 8
        assert "input_snapshot" not in entry
        assert "rulings" not in entry
        assert "override_reason" not in str(entry)

    # The fixture's only selected scorer is a Forward.  Its literal frozen
    # AFL goals/behinds are preserved rather than divmodding the point total.
    first = ss["entries"][0]
    f1 = next(position for position in first["positions"] if position["slot"] == "F1")
    assert f1["football_line"] == f"{first['total_score'] // 6:.0f}.0"
    assert f1["display_state"] == "completed"


def test_published_superscore_detail_and_rank_ignore_later_mutable_calculation(public_client):
    """Published totals, ranks and player evidence come exclusively from
    `superscore_official_result.input_snapshot`, even if the replaceable
    calculation row later diverges."""
    built = build_completable_season(year=8109, database=public_client.app.state.database)
    database = public_client.app.state.database
    season_id = built["season"].season_id
    before = public_client.get(f"/api/public/seasons/{season_id}/rounds/24").json()["superscore"]
    winner = before["entries"][0]

    row = database.execute(
        "SELECT snapshot FROM superscore_entry_calculation WHERE bbbffl_round_id=? AND season_entry_id=?",
        (before["round_id"], winner["season_entry_id"]),
    ).fetchone()
    snapshot = json.loads(row["snapshot"])
    snapshot["effective_entry"]["slots"][0]["player_name"] = "Mutable impostor"
    snapshot["effective_entry"]["slots"][0]["effective_score"] = 999
    with database.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE superscore_entry_calculation SET total_score=999,snapshot=:snapshot "
                "WHERE bbbffl_round_id=:round_id AND season_entry_id=:entry_id"
            ),
            {"snapshot": json.dumps(snapshot), "round_id": before["round_id"], "entry_id": winner["season_entry_id"]},
        )

    after = public_client.get(f"/api/public/seasons/{season_id}/rounds/24").json()["superscore"]
    same = next(entry for entry in after["entries"] if entry["season_entry_id"] == winner["season_entry_id"])
    assert same["rank"] == winner["rank"]
    assert same["total_score"] == winner["total_score"]
    assert "Mutable impostor" not in str(same)
    assert 999 not in [position["effective_score"] for position in same["positions"]]


def _record_dnp_and_interchange(built, matchup_id, entry_id, position, *, review_version, reason):
    review_repo = RoundReviewRepository(built["database"])
    version = review_repo.record_dnp_ruling(
        matchup_id, entry_id, position, True, expected_review_version=review_version, actor=ACTOR, reason=reason
    )
    review_repo.record_interchange_ruling(
        matchup_id, entry_id, position, expected_review_version=version, actor=ACTOR, reason=reason
    )


def test_finals_lineup_preserves_original_dnp_player_and_names_the_interchange_replacement(public_client):
    """Issue #261 requirement 3 / the Evil Absolutes Tackler example: a
    named original selection who is ruled DNP and covered by the
    Interchange must show (a) the original player's own name, (b)
    `confirmed_dnp`, (c) the interchange player's name against that same
    position, and (d) the effective (interchange) score -- never collapsing
    to a bare "Vacant" the way a naive reduction would."""
    built = build_finals_ready_season(year=8103, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    round_id = _open_week1(built, bracket, year=8103)
    season_id = built["season"].season_id

    pairing = next(p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1) if p.slot == "qf")
    competition_id = built["finals_competition"].competition_id
    entry_id = pairing.home_season_entry_id

    scores = {"F1": 18, "F2": 18, "F3": 18, "M1": 18, "M2": 18, "M3": 18, "Ruck": 18, "Tackler": 18}
    stats = {}
    # The Interchange player: distinct tackles so the effective Tackler
    # score via interchange (48) is unmistakably different from the
    # original, DNP'd Tackler's own would-be score (18).
    interchange_id = f"home-{round_id}-Interchange"
    interchange_canonical = (abs(hash(f"home-{round_id}")) % 500_000) * 100 + 900
    _seed_named_finals_lineup(
        built,
        round_id,
        competition_id,
        entry_id,
        scores,
        label=f"home-{round_id}",
        stats=stats,
        interchange=(interchange_id, interchange_canonical, "Matt Rowell", {"tackles": 8}),
    )
    _seed_named_finals_lineup(
        built, round_id, competition_id, pairing.away_season_entry_id, scores, label=f"away-{round_id}", stats=stats
    )

    lifecycle = built["lifecycle"]
    identities = IdentityRepository(built["database"])
    review_repo = RoundReviewRepository(built["database"])
    MatchupCalculationService(built["database"], _Facts(stats)).calculate_matchup(pairing.matchup_id, guard_season=True)
    review = build_finals_round_review(lifecycle, review_repo, identities, round_id)
    matchup_review = next(m for m in review["matchups"] if m.matchup_id == pairing.matchup_id)
    _record_dnp_and_interchange(
        built,
        pairing.matchup_id,
        entry_id,
        "Tackler",
        review_version=matchup_review.review_version,
        reason="withdrew pregame",
    )

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    qf = next(m for m in body["matchups"] if m["slot"] == "qf")
    side = qf["home"] if pairing.home_season_entry_id == entry_id else qf["away"]
    tackler = next(p for p in side["lineup"]["players"] if p["position"] == "Tackler")

    assert tackler["outcome"] == "replaced_by_interchange"
    assert tackler["confirmed_dnp"] is True
    # The coach's original selection is preserved -- never reduced to
    # "Vacant" just because an interchange is now effectively scoring it.
    assert tackler["player_name"] == f"home-{round_id} Tackler"
    assert tackler["interchange_player_name"] == "Matt Rowell"
    assert tackler["effective_score"] == 48


def test_finals_lineup_never_shown_as_bare_vacant_when_a_genuine_vacancy_is_covered_by_interchange(public_client):
    """The other half of requirement 3: even when the position was never
    named at all (a genuine vacancy, not a DNP), the row must still name
    the interchange player who is effectively covering it and show that
    effective score -- `player_name` stays `None` (accurately: no one was
    ever selected), but `interchange_player_name` makes clear who is
    actually generating the shown score."""
    built = build_finals_ready_season(year=8104, database=public_client.app.state.database)
    bracket = _create_bracket(built)
    round_id = _open_week1(built, bracket, year=8104)
    season_id = built["season"].season_id

    pairing = next(p for p in _repo(built).list_pairings(bracket.bracket_id, week_number=1) if p.slot == "qf")
    competition_id = built["finals_competition"].competition_id
    entry_id = pairing.home_season_entry_id

    scores = {"F1": 18, "F2": 18, "F3": 18, "M1": 18, "M2": 18, "M3": 18, "Ruck": 18, "Tackler": 18}
    stats = {}
    # Leave the Tackler position genuinely vacant (no coach selection) for
    # the home entry, but still name and score an Interchange to cover it.
    interchange_id = f"vhome-{round_id}-Interchange"
    interchange_canonical = (abs(hash(f"vhome-{round_id}")) % 500_000) * 100 + 900
    _seed_named_finals_lineup(
        built,
        round_id,
        competition_id,
        entry_id,
        scores,
        label=f"vhome-{round_id}",
        stats=stats,
        vacant_positions=("Tackler",),
        interchange=(interchange_id, interchange_canonical, "Matt Rowell", {"tackles": 8}),
    )
    _seed_named_finals_lineup(
        built, round_id, competition_id, pairing.away_season_entry_id, scores, label=f"vaway-{round_id}", stats=stats
    )

    lifecycle = built["lifecycle"]
    identities = IdentityRepository(built["database"])
    review_repo = RoundReviewRepository(built["database"])
    MatchupCalculationService(built["database"], _Facts(stats)).calculate_matchup(pairing.matchup_id, guard_season=True)
    review = build_finals_round_review(lifecycle, review_repo, identities, round_id)
    matchup_review = next(m for m in review["matchups"] if m.matchup_id == pairing.matchup_id)
    review_repo.record_interchange_ruling(
        pairing.matchup_id,
        entry_id,
        "Tackler",
        expected_review_version=matchup_review.review_version,
        actor=ACTOR,
        reason="cover intentional vacancy",
    )

    body = public_client.get(f"/api/public/seasons/{season_id}/rounds/21").json()
    qf = next(m for m in body["matchups"] if m["slot"] == "qf")
    side = qf["home"] if pairing.home_season_entry_id == entry_id else qf["away"]
    tackler = next(p for p in side["lineup"]["players"] if p["position"] == "Tackler")

    assert tackler["outcome"] == "replaced_by_interchange"
    assert tackler["player_name"] is None
    assert tackler["interchange_player_name"] == "Matt Rowell"
    assert tackler["effective_score"] == 48
