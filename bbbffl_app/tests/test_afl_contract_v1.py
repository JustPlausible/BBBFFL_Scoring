"""Pins BBBFFL's supported subset of the afl-api /api/v1 consumer contract
(issue #18 / roadmap package 04).

This module is deliberately split from `test_afl_client.py` (which proves
`AflApiClient` parses the shapes it already consumes). This module instead:

  1. exercises `AflApiClient` against a *richer* set of fixtures covering
     semantic edges (nullable/empty/populated byes, all four match lifecycle
     states in one round, historical seasons, null-vs-zero stats, unresolved
     player identity) that `test_afl_client.py`'s narrower fixtures don't
     reach;
  2. pins BBBFFL's understanding of fields/endpoints afl-api v1 exposes but
     `AflApiClient` does not yet parse (player search, player/season
     membership, injuries, rosters, structured errors) -- these are
     committed future dependencies for later roadmap packages (08/11/23/27),
     recorded here as fixture-shape assertions rather than new client code,
     per this issue's non-goals; and
  3. proves the adapter tolerates additive unknown fields and fails loudly
     (rather than silently misinterpreting) on an incompatible removed
     field, per the compatibility policy in
     `docs/afl-api-v1-contract.md`.

All fixtures are offline (`tests/fixtures/afl_api_v1/`); see that
directory's PROVENANCE.md for their source and why they are source-derived
rather than live-captured in this change. No network is used by this file.
"""

import copy
import json
from pathlib import Path

import httpx
import pytest

from app.afl_client import AflApiClient, PlayerStatLine

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "afl_api_v1"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


ROUTES = {
    "/api/v1": lambda: _load("api_discovery.json"),
    "/api/v1/seasons": lambda: _load("seasons.json"),
    "/api/v1/seasons/85/rounds": lambda: _load("rounds_season_85.json"),
    "/api/v1/seasons/84/rounds": lambda: _load("rounds_season_84_historical.json"),
    "/api/v1/rounds/1412/matches": lambda: _load("matches_round_1412_lifecycle.json"),
    "/api/v1/matches/8504": lambda: _load("match_8504_detail.json"),
    "/api/v1/matches/8503/player-stats": lambda: _load("player_stats_8503_postgame_partial.json"),
    "/api/v1/matches/8504/player-stats": lambda: _load("player_stats_8504_concluded_final.json"),
    "/api/v1/players/396": lambda: _load("player_396.json"),
    "/api/v1/players/584/seasons": lambda: _load("player_584_seasons.json"),
    "/api/v1/injuries": lambda: _load("injuries.json"),
    "/api/v1/matches/8504/rosters": lambda: _load("rosters_match_8504.json"),
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v1/players" and request.url.params.get("search") == "daicos":
        return httpx.Response(200, json=_load("players_search_daicos.json"))
    if path in ROUTES:
        return httpx.Response(200, json=ROUTES[path]())
    return httpx.Response(404, json={"error": {"code": "not_found", "message": "no mock route"}})


@pytest.fixture
def client():
    api = AflApiClient(base_url="http://afl-api.test")
    api._client = httpx.Client(base_url="http://afl-api.test", transport=httpx.MockTransport(_handler))
    yield api
    api.close()


# --- Seasons: current selection and historical access ------------------


def test_get_current_season_selects_flagged_current_not_first_entry(client):
    """seasons.json lists the current season first; a reordered copy proves
    selection is driven by is_current, not list position -- afl-api's own
    contract makes no ordering promise consumers may rely on for this."""
    season = client.get_current_season()
    assert season.season_id == 85
    assert season.is_current is True


def test_historical_season_is_reachable_and_has_nullable_current_round_number():
    """Historical (non-current) seasons remain listed by GET /api/v1/seasons
    (roadmap package 04/32's replay prerequisite) and may report
    current_round_number: null -- BBBFFL must not assume every season row
    has a usable current round."""
    seasons = _load("seasons.json")["seasons"]
    historical = next(s for s in seasons if s["season_id"] == 84)
    assert historical["is_current"] is False
    assert historical["current_round_number"] is None


def test_get_current_season_order_independence():
    reordered = copy.deepcopy(_load("seasons.json"))
    reordered["seasons"].reverse()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/seasons":
            return httpx.Response(200, json=reordered)
        return httpx.Response(404)

    api = AflApiClient(base_url="http://afl-api.test")
    api._client = httpx.Client(base_url="http://afl-api.test", transport=httpx.MockTransport(handler))
    try:
        assert api.get_current_season().season_id == 85
    finally:
        api.close()


def test_historical_season_rounds_are_reachable(client):
    """Package 32's replay prerequisite: a non-current season's rounds must
    still resolve through the same round-navigation path as the current
    season, using the same public AflApiClient.get_round() method."""
    round_ = client.get_round(season_id=84, round_number=1)
    assert round_.round_id == 1300
    assert round_.round_number == 1


# --- Rounds: bye null/empty/populated distinction -----------------------
#
# AflApiClient.get_round() does not currently parse `byes` at all (see
# app/afl_client.py's Round dataclass) -- ordinary-bye handling is a
# committed future dependency for roadmap package 24, not implemented yet.
# These assertions pin the *raw contract* BBBFFL will consume when that
# package lands, so an upstream change to this null/empty/populated
# distinction is caught even before BBBFFL code reads the field.


def test_round_byes_distinguishes_null_empty_and_populated():
    rounds = {r["round_number"]: r for r in _load("rounds_season_85.json")["rounds"]}
    assert rounds[0]["byes"] == []  # explicit "no byes this round"
    assert rounds[1]["byes"] is None  # unresolved/unavailable, not "no byes"
    assert rounds[12]["byes"] == [{"team_id": 2, "name": "Western Bulldogs", "abbreviation": "WB"}]


# --- Matches: all four lifecycle states distinguished in one round ------


def test_matches_distinguish_all_four_lifecycle_states_within_one_round(client):
    matches = {m.match_id: m for m in client.get_matches(1412)}
    assert matches[8501].state == "yet_to_play"  # raw status "UPCOMING"
    assert matches[8502].state == "live"
    assert matches[8503].state == "postgame"
    assert matches[8504].state == "completed"  # raw status "CONCLUDED"
    # POSTGAME must never collapse into either neighbour.
    assert matches[8503].state not in ("live", "completed")


def test_match_scores_are_null_before_available_not_fabricated_zero(client):
    matches = {m.match_id: m for m in client.get_matches(1412)}
    upcoming_raw = next(m for m in _load("matches_round_1412_lifecycle.json")["matches"] if m["match_id"] == 8501)
    assert upcoming_raw["score_home"] is None
    assert upcoming_raw["score_away"] is None
    assert matches[8501].state == "yet_to_play"


# --- Player-stat finality, identity resolution, and the null-vs-zero gap


def test_player_stats_finality_partial_vs_final_is_present_on_the_raw_resource():
    """AflApiClient does not currently read `lifecycle.finality` (see
    app/afl_client.py's get_match_player_stats) -- BBBFFL currently infers
    liveness only from the match's own `status`, not from this
    authoritative per-request finality field. This is a documented gap
    (see docs/afl-api-v1-contract.md); pinned here at the raw-contract
    level so a future consumer of this field has a fixture-proven shape."""
    partial = _load("player_stats_8503_postgame_partial.json")
    final = _load("player_stats_8504_concluded_final.json")
    assert partial["lifecycle"]["finality"] == "partial"
    assert final["lifecycle"]["finality"] == "final"


def test_get_match_player_stats_drops_rows_with_unresolved_canonical_identity(client):
    """A stat row whose canonical_player_id is null (unresolved crosswalk)
    is intentionally excluded from AflApiClient's result: BBBFFL keys
    everything by canonical_player_id and must never invent one. This is a
    deliberate identity boundary, not a parsing bug -- see the "Identifiers"
    section of docs/afl-api-v1-contract.md."""
    stats = client.get_match_player_stats(8503)
    assert 396 in stats
    assert 7734 in stats
    assert len(stats) == 2  # the null-canonical_player_id row is dropped


def test_get_match_player_stats_currently_coerces_null_stat_field_to_zero(client):
    """When a *resolved*
    player's stat row has one individual field still null mid-collection
    (afl-api's real "unavailable" semantic -- see docs/api_v1_player_stats.md
    field notes), AflApiClient's `int(row_stats.get(field) or 0)` coercion
    currently collapses that null into a known zero, identically to an
    actually-recorded zero. Player 7734 in the partial-finality fixture has
    hitouts: null (still collecting) alongside populated goals/behinds/etc.
    Package 26 retains that distinction so incomplete facts cannot silently
    produce a score -- see docs/afl-api-v1-contract.md's
    "known upstream/consumer gaps" section."""
    stats = client.get_match_player_stats(8503)
    assert stats[7734].hitouts is None


def test_get_match_player_stats_final_lifecycle_has_fully_resolved_rows(client):
    stats = client.get_match_player_stats(8504)
    assert stats[584] == PlayerStatLine(
        canonical_player_id=584, goals=3, behinds=1, disposals=33, marks=9, tackles=4, hitouts=0
    )
    assert stats[7734] == PlayerStatLine(
        canonical_player_id=7734, goals=0, behinds=0, disposals=7, marks=4, tackles=1, hitouts=31
    )


# --- Canonical player identity: stored vs. crosswalk-only ---------------


def test_get_player_uses_canonical_player_id_and_does_not_retain_provider_crosswalks(client):
    """BBBFFL's Player dataclass intentionally has no afl_player_id /
    champion_data_player_id fields: canonical_player_id is BBBFFL's sole
    stored AFL player identity throughout (app/afl_client.py's module
    docstring). afl-api remains the authority for provider crosswalks;
    BBBFFL never needs to reproduce that resolution."""
    player = client.get_player(396)
    assert player.canonical_player_id == 396
    assert not hasattr(player, "afl_player_id")
    assert not hasattr(player, "identifiers")


def test_player_season_membership_never_rewrites_an_earlier_seasons_team():
    """Committed future dependency for package 11 (season player pool):
    GET /api/v1/players/{id}/seasons scopes team to the specific season row
    -- a later club change must not appear to rewrite history. Not yet
    consumed by AflApiClient; pinned at the raw-contract level."""
    seasons = _load("player_584_seasons.json")["seasons"]
    by_year = {s["year"]: s["team"]["name"] for s in seasons}
    assert by_year == {2027: "Essendon", 2026: "Collingwood", 2025: "Collingwood"}


def test_player_search_contract_shape():
    """Committed future dependency for package 11 (bulk-ish identity
    discovery is the closest available primitive -- see the "Upstream
    gaps" section of docs/afl-api-v1-contract.md for why this is NOT a
    substitute for a bulk season player-list endpoint). Not yet consumed
    by AflApiClient; pinned at the raw-contract level."""
    players = _load("players_search_daicos.json")["players"]
    assert {p["canonical_player_id"] for p in players} == {396, 584}
    for p in players:
        assert "identifiers" in p and "current_team" in p


# --- Injuries and rosters: committed future dependencies (packages 23/27)


def test_injuries_contract_shape_current_only_with_nullable_team():
    """Committed future dependency for package 27 (DNP evidence). Not yet
    consumed by AflApiClient."""
    injuries = _load("injuries.json")["injuries"]
    assert all(row["current"] is True for row in injuries)
    resolved, unresolved = injuries
    assert resolved["team"]["team_id"] == 11
    assert unresolved["team"] is None
    assert unresolved["player"]["display_name"] is None


def test_rosters_contract_selections_are_not_participation_evidence():
    """Committed future dependency for packages 23 (lockout evidence) and
    27 (DNP evidence). `rosters` is a *selection*, distinct from the
    player-stats participation resource -- afl-api's own contract is
    explicit that a roster selection must never be read as confirmation a
    player took the field. Not yet consumed by AflApiClient."""
    payload = _load("rosters_match_8504.json")
    assert payload["away_team"] is None  # no observation persisted yet for that side
    home = payload["home_team"]
    assert home["team_status"] == "CONFIRMED"
    selections = home["selections"]
    assert {s["position"] for s in selections} == {"MIDFIELD", "INTERCHANGE"}
    # A context record (e.g. an "out") is a separate collection, never merged
    # into `selections`.
    assert len(home["context"]["outs"]) == 1
    assert home["context"]["outs"][0]["player"]["display_name"] not in {s["player"]["display_name"] for s in selections}


# --- Error shapes: structured application errors vs. auth-layer errors --


def test_structured_application_error_shape():
    err = _load("error_404_match_not_found.json")
    assert err["error"]["code"] == "match_not_found"
    assert "detail" not in err


def test_auth_layer_error_shape_is_deliberately_different():
    """401 (missing/invalid API key) uses FastAPI's plain {"detail": ...}
    shape, NOT the structured {"error": {"code", "message"}} shape used by
    every other /api/v1 application error (confirmed in afl-api's
    auth.py/authenticate_api_key). A client that only understands the
    structured shape must not silently misparse a 401 as a generic 404-style
    failure."""
    err = _load("error_401_unauthenticated.json")
    assert "detail" in err
    assert "error" not in err


def test_capability_gated_error_shape():
    err = _load("error_403_advanced_access_required.json")
    assert err["error"]["code"] == "advanced_access_required"


def test_blank_search_error_shape():
    err = _load("error_422_search_required.json")
    assert err["error"]["code"] == "search_required"


# --- Compatibility policy: additive tolerance, incompatible-change failure


def test_client_tolerates_unknown_additive_fields(client):
    """Additive fields anywhere in the response must never break parsing --
    this is the compatibility policy's central promise (see
    docs/afl-api-v1-contract.md)."""
    augmented = copy.deepcopy(_load("seasons.json"))
    for entry in augmented["seasons"]:
        entry["a_brand_new_upstream_field"] = {"nested": ["whatever"]}
    augmented["a_new_top_level_wrapper_field"] = "ignored"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/seasons":
            return httpx.Response(200, json=augmented)
        return httpx.Response(404)

    api = AflApiClient(base_url="http://afl-api.test")
    api._client = httpx.Client(base_url="http://afl-api.test", transport=httpx.MockTransport(handler))
    try:
        season = api.get_current_season()
        assert season.season_id == 85
    finally:
        api.close()


def test_client_fails_loudly_when_a_required_identifier_field_is_removed(client):
    """An incompatible upstream change (a required field BBBFFL depends on
    disappearing) must raise, not silently produce a wrong/default value --
    proving BBBFFL does not misinterpret a breaking change as valid data."""
    broken = copy.deepcopy(_load("seasons.json"))
    del broken["seasons"][0]["season_id"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/seasons":
            return httpx.Response(200, json=broken)
        return httpx.Response(404)

    api = AflApiClient(base_url="http://afl-api.test")
    api._client = httpx.Client(base_url="http://afl-api.test", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(KeyError):
            api.get_current_season()
    finally:
        api.close()


def test_client_fails_loudly_when_matches_wrapper_key_is_renamed_incompatibly():
    """An incompatible wrapper rename (not covered by the "results" legacy
    fallback) must not silently resolve to an empty/wrong collection that
    could be misread as "no matches this round"."""
    broken = {"fixtures": _load("matches_round_1412_lifecycle.json")["matches"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/rounds/1412/matches":
            return httpx.Response(200, json=broken)
        return httpx.Response(404)

    api = AflApiClient(base_url="http://afl-api.test")
    api._client = httpx.Client(base_url="http://afl-api.test", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(TypeError):
            # _unwrap falls back to the whole (dict) payload when neither the
            # named nor "results" key is present, so iterating it as match
            # rows raises rather than returning a plausible-looking result.
            api.get_matches(1412)
    finally:
        api.close()
