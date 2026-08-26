"""Hermetic self-test for scripts/afl_contract_diagnostic.py's own check
logic (issue #18). This does NOT touch the network -- it proves the
diagnostic correctly classifies a well-formed mock deployment as passing
and correctly detects an unauthenticated/invalid-key request, using
httpx.MockTransport exactly like the rest of the offline test suite.

The diagnostic's actual value (validating a *real* deployment) is
necessarily untested here -- that is what running it live, opt-in, against
a configured afl-api instance is for. See docs/afl-api-v1-contract.md for
how to run it.
"""

import httpx

from scripts.afl_contract_diagnostic import run

VALID_KEY = "test-diagnostic-key"

DISCOVERY = {"name": "AFL-api", "version": "0.7.0", "documentation": "/docs"}
SEASONS = {
    "seasons": [
        {"season_id": 85, "year": 2026, "name": "2026", "is_current": True, "current_round_number": 1},
        {"season_id": 84, "year": 2025, "name": "2025", "is_current": False, "current_round_number": None},
    ]
}
ROUNDS_85 = {"rounds": [{"round_id": 1, "season_id": 85, "round_number": 1, "name": "Round 1",
                         "abbreviation": "R1", "start_time": None, "end_time": None, "byes": []}]}
ROUNDS_84 = {"rounds": [{"round_id": 2, "season_id": 84, "round_number": 1, "name": "Round 1",
                         "abbreviation": "R1", "start_time": None, "end_time": None, "byes": None}]}
MATCHES = {
    "matches": [
        {"match_id": 100, "round_id": 1, "season_id": 85, "status": "CONCLUDED",
         "start_time_utc": "2026-03-14T00:00:00Z",
         "home_team": {"team_id": 1, "name": "Home"}, "away_team": {"team_id": 2, "name": "Away"},
         "score_home": 90, "score_away": 60},
        {"match_id": 101, "round_id": 1, "season_id": 85, "status": "UPCOMING",
         "start_time_utc": "2026-03-21T00:00:00Z",
         "home_team": {"team_id": 3, "name": "Third"}, "away_team": {"team_id": 4, "name": "Fourth"},
         "score_home": None, "score_away": None},
    ]
}
MATCH_100 = MATCHES["matches"][0]
PLAYER_STATS_100 = {
    "match": {"match_id": 100, "match_provider_id": "CD_M1", "round_id": 1, "season_id": 85, "status": "CONCLUDED"},
    "lifecycle": {"finality": "final"},
    "metadata": {"source_updated_at": "2026-03-14T02:00:00Z"},
    "players": [
        {
            "champion_data_player_id": "CD_I1", "canonical_player_id": 1, "afl_player_id": 100,
            "display_name": "Test Player", "side": "home", "team_id": 1,
            "stats": {"goals": 2, "behinds": 1, "kicks": 10, "handballs": 5, "disposals": 15,
                      "marks": 3, "tackles": 2, "hitouts": 0},
        }
    ],
}
PLAYER_1 = {
    "player": {
        "canonical_player_id": 1, "display_name": "Test Player",
        "current_team": {"team_id": 1, "name": "Home"},
        "identifiers": {"afl_player_id": 100, "champion_data_player_id": "CD_I1"},
    }
}
PLAYER_1_SEASONS = {"canonical_player_id": 1, "seasons": [{"season_id": 85, "year": 2026, "name": "2026",
                    "team": {"team_id": 1, "name": "Home"}}]}
PLAYERS_SEARCH = {"players": [PLAYER_1["player"]]}
INJURIES = {"injuries": []}
ROSTERS_100 = {
    "match": MATCH_100,
    "metadata": {"match_status_at_observation": None, "source_updated_at": None},
    "home_team": None,
    "away_team": None,
}
ERROR_404 = {"error": {"code": "player_not_found", "message": "Player not found."}}
ERROR_422 = {"error": {"code": "search_required", "message": "A non-blank search query parameter is required."}}
ERROR_401 = {"detail": "Invalid or missing API Key"}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    key = request.headers.get("x-api-key")

    # Error-shape checks intentionally query with a valid key but a
    # deliberately unresolvable/invalid parameter -- handle those before the
    # generic key gate below, same as the real service would (auth still
    # required, but these are 404/422 application errors, not 401s).
    if key == VALID_KEY and path == "/api/v1/players/999999999999":
        return httpx.Response(404, json=ERROR_404)
    if key == VALID_KEY and path == "/api/v1/players" and request.url.params.get("search") == "":
        return httpx.Response(422, json=ERROR_422)

    if key != VALID_KEY:
        return httpx.Response(401, json=ERROR_401)

    routes = {
        "/api/v1": DISCOVERY,
        "/api/v1/seasons": SEASONS,
        "/api/v1/seasons/85/rounds": ROUNDS_85,
        "/api/v1/seasons/84/rounds": ROUNDS_84,
        "/api/v1/rounds/1/matches": MATCHES,
        "/api/v1/matches/100": MATCH_100,
        "/api/v1/matches/100/player-stats": PLAYER_STATS_100,
        "/api/v1/players/1": PLAYER_1,
        "/api/v1/players/1/seasons": PLAYER_1_SEASONS,
        "/api/v1/injuries": INJURIES,
        "/api/v1/matches/100/rosters": ROSTERS_100,
    }
    if path == "/api/v1/players" and request.url.params.get("search"):
        return httpx.Response(200, json=PLAYERS_SEARCH)
    if path == "/openapi.json":
        return httpx.Response(404)  # optional check should SKIP, not fail the run
    if path in routes:
        return httpx.Response(200, json=routes[path])
    return httpx.Response(404, json={"error": {"code": "not_found", "message": "no mock route"}})


def test_diagnostic_passes_every_required_check_against_a_well_formed_mock_deployment():
    transport = httpx.MockTransport(_handler)
    results = run("http://afl-api.test", VALID_KEY, transport=transport)

    required_failures = [r for r in results if r.required and r.status == "FAIL"]
    assert required_failures == [], [(r.name, r.detail) for r in required_failures]
    assert any(r.name.startswith("GET /api/v1 (discovery)") and r.status == "PASS" for r in results)
    assert any("no key" in r.name and r.status == "PASS" for r in results)
    assert any("invalid key" in r.name and r.status == "PASS" for r in results)
    assert any("structured 404" in r.name and r.status == "PASS" for r in results)
    assert any("structured 422" in r.name and r.status == "PASS" for r in results)
    # The optional OpenAPI check SKIPs cleanly (404 above) rather than
    # failing the whole diagnostic -- BBBFFL's runtime never requires it.
    openapi = next(r for r in results if r.name.startswith("GET /openapi.json"))
    assert openapi.status == "SKIP"
    assert openapi.required is False


def test_diagnostic_detects_a_deployment_that_never_accepts_the_configured_key():
    def always_401(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=ERROR_401)

    results = run("http://afl-api.test", VALID_KEY, transport=httpx.MockTransport(always_401))
    required_failures = [r for r in results if r.required and r.status == "FAIL"]
    assert required_failures, "an all-401 deployment must fail at least one required check"
