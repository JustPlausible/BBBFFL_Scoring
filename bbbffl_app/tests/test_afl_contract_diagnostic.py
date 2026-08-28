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

import copy

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
ROUNDS_85 = {
    "rounds": [
        {
            "round_id": 1,
            "season_id": 85,
            "round_number": 1,
            "name": "Round 1",
            "abbreviation": "R1",
            "start_time": None,
            "end_time": None,
            "byes": [],
        }
    ]
}
ROUNDS_84 = {
    "rounds": [
        {
            "round_id": 2,
            "season_id": 84,
            "round_number": 1,
            "name": "Round 1",
            "abbreviation": "R1",
            "start_time": None,
            "end_time": None,
            "byes": None,
        }
    ]
}
MATCHES = {
    "matches": [
        {
            "match_id": 100,
            "round_id": 1,
            "season_id": 85,
            "status": "CONCLUDED",
            "start_time_utc": "2026-03-14T00:00:00Z",
            "home_team": {"team_id": 1, "name": "Home"},
            "away_team": {"team_id": 2, "name": "Away"},
            "score_home": 90,
            "score_away": 60,
        },
        {
            "match_id": 101,
            "round_id": 1,
            "season_id": 85,
            "status": "UPCOMING",
            "start_time_utc": "2026-03-21T00:00:00Z",
            "home_team": {"team_id": 3, "name": "Third"},
            "away_team": {"team_id": 4, "name": "Fourth"},
            "score_home": None,
            "score_away": None,
        },
    ]
}
MATCH_100 = MATCHES["matches"][0]
PLAYER_STATS_100 = {
    "match": {"match_id": 100, "match_provider_id": "CD_M1", "round_id": 1, "season_id": 85, "status": "CONCLUDED"},
    "lifecycle": {"finality": "final"},
    "metadata": {"source_updated_at": "2026-03-14T02:00:00Z"},
    "players": [
        {
            "champion_data_player_id": "CD_I1",
            "canonical_player_id": 1,
            "afl_player_id": 100,
            "display_name": "Test Player",
            "side": "home",
            "team_id": 1,
            "stats": {
                "goals": 2,
                "behinds": 1,
                "kicks": 10,
                "handballs": 5,
                "disposals": 15,
                "marks": 3,
                "tackles": 2,
                "hitouts": 0,
            },
        }
    ],
}
PLAYER_1 = {
    "player": {
        "canonical_player_id": 1,
        "display_name": "Test Player",
        "current_team": {"team_id": 1, "name": "Home"},
        "identifiers": {"afl_player_id": 100, "champion_data_player_id": "CD_I1"},
    }
}
PLAYER_1_SEASONS = {
    "canonical_player_id": 1,
    "seasons": [{"season_id": 85, "year": 2026, "name": "2026", "team": {"team_id": 1, "name": "Home"}}],
}
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


def _not_fully_validated(results):
    """Mirrors _print_report's exit-code logic: a required FAIL or a
    required SKIP both mean the contract was not actually confirmed."""
    return [r for r in results if r.required and r.status != "PASS"]


def test_diagnostic_reports_a_malformed_200_response_as_a_failed_check_not_a_crash():
    """A deployment that returns syntactically valid JSON with a required
    field missing (e.g. season_id absent from the is_current season row)
    must not crash the whole diagnostic with an unhandled KeyError -- it
    should be recorded as a failed check, and every other check should
    still run and report."""
    broken_seasons = copy.deepcopy(SEASONS)
    del broken_seasons["seasons"][0]["season_id"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != VALID_KEY:
            return httpx.Response(401, json=ERROR_401)
        if request.url.path == "/api/v1":
            return httpx.Response(200, json=DISCOVERY)
        if request.url.path == "/api/v1/seasons":
            return httpx.Response(200, json=broken_seasons)
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no mock route"}})

    results = run("http://afl-api.test", VALID_KEY, transport=httpx.MockTransport(handler))
    seasons_check = next(r for r in results if r.name.startswith("check_seasons"))
    assert seasons_check.status == "FAIL"
    assert "malformed response" in seasons_check.name
    assert "KeyError" in seasons_check.detail
    # Later checks still ran (skipped for lack of a resolved season) rather
    # than the whole run aborting.
    assert any(r.name.startswith("GET /api/v1/seasons/{id}/rounds") for r in results)
    assert _not_fully_validated(results)


def test_diagnostic_flags_a_later_player_row_missing_a_scored_stat_field():
    broken_stats = copy.deepcopy(PLAYER_STATS_100)
    second_row = copy.deepcopy(PLAYER_STATS_100["players"][0])
    second_row["canonical_player_id"] = 2
    second_row["champion_data_player_id"] = "CD_I2"
    del second_row["stats"]["hitouts"]
    broken_stats["players"].append(second_row)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != VALID_KEY:
            return httpx.Response(401, json=ERROR_401)
        if request.url.path == "/api/v1/matches/100/player-stats":
            return httpx.Response(200, json=broken_stats)
        routes = {
            "/api/v1": DISCOVERY,
            "/api/v1/seasons": SEASONS,
            "/api/v1/seasons/85/rounds": ROUNDS_85,
            "/api/v1/seasons/84/rounds": ROUNDS_84,
            "/api/v1/rounds/1/matches": MATCHES,
            "/api/v1/matches/100": MATCH_100,
        }
        if request.url.path in routes:
            return httpx.Response(200, json=routes[request.url.path])
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no mock route"}})

    results = run("http://afl-api.test", VALID_KEY, transport=httpx.MockTransport(handler))
    stat_field_check = next(r for r in results if r.name == "player-stats: rows expose all BBBFFL-scored stat fields")
    assert stat_field_check.status == "FAIL"
    assert "1" in stat_field_check.detail  # row_index of the broken second row
    assert "hitouts" in stat_field_check.detail


def test_diagnostic_selects_the_round_matching_current_round_number_not_the_first_round():
    rounds = {
        "rounds": [
            {
                "round_id": 10,
                "season_id": 85,
                "round_number": 1,
                "name": "Round 1",
                "abbreviation": "R1",
                "start_time": None,
                "end_time": None,
                "byes": [],
            },
            {
                "round_id": 99,
                "season_id": 85,
                "round_number": 5,
                "name": "Round 5",
                "abbreviation": "R5",
                "start_time": None,
                "end_time": None,
                "byes": [],
            },
        ]
    }
    seasons = copy.deepcopy(SEASONS)
    seasons["seasons"][0]["current_round_number"] = 5
    matches_for_current_round = {
        "matches": [
            {
                "match_id": 200,
                "round_id": 99,
                "season_id": 85,
                "status": "CONCLUDED",
                "start_time_utc": None,
                "home_team": {"team_id": 1, "name": "Home"},
                "away_team": {"team_id": 2, "name": "Away"},
                "score_home": 50,
                "score_away": 40,
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != VALID_KEY:
            return httpx.Response(401, json=ERROR_401)
        path = request.url.path
        if path == "/api/v1/seasons":
            return httpx.Response(200, json=seasons)
        if path == "/api/v1/seasons/85/rounds":
            return httpx.Response(200, json=rounds)
        if path == "/api/v1/rounds/10/matches":
            # The old (wrong) "first round" pick -- must NOT be queried as
            # the diagnostic's primary selection.
            return httpx.Response(
                200,
                json={
                    "matches": [
                        {
                            "match_id": 999,
                            "round_id": 10,
                            "season_id": 85,
                            "status": "UPCOMING",
                            "start_time_utc": None,
                            "home_team": {"team_id": 3, "name": "Third"},
                            "away_team": {"team_id": 4, "name": "Fourth"},
                            "score_home": None,
                            "score_away": None,
                        }
                    ]
                },
            )
        if path == "/api/v1/rounds/99/matches":
            return httpx.Response(200, json=matches_for_current_round)
        if path == "/api/v1":
            return httpx.Response(200, json=DISCOVERY)
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no mock route"}})

    results = run("http://afl-api.test", VALID_KEY, transport=httpx.MockTransport(handler))
    matches_check = next(r for r in results if r.name == "GET /api/v1/rounds/{id}/matches")
    assert matches_check.status == "PASS"
    assert "1 matches" in matches_check.detail  # round 99's single match, not round 10's


def test_diagnostic_treats_a_required_skip_as_not_fully_validated():
    """A deployment with only one season (no historical season to probe)
    leaves a REQUIRED check unable to do more than SKIP -- that must not
    be reported as equivalent to a pass."""
    single_season = {"seasons": [SEASONS["seasons"][0]]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != VALID_KEY:
            return httpx.Response(401, json=ERROR_401)
        path = request.url.path
        if path == "/api/v1/seasons":
            return httpx.Response(200, json=single_season)
        if path == "/api/v1/players/999999999999":
            return httpx.Response(404, json=ERROR_404)
        routes = {
            "/api/v1": DISCOVERY,
            "/api/v1/seasons/85/rounds": ROUNDS_85,
            "/api/v1/rounds/1/matches": MATCHES,
            "/api/v1/matches/100": MATCH_100,
            "/api/v1/matches/100/player-stats": PLAYER_STATS_100,
            "/api/v1/players/1": PLAYER_1,
            "/api/v1/players/1/seasons": PLAYER_1_SEASONS,
            "/api/v1/injuries": INJURIES,
            "/api/v1/matches/100/rosters": ROSTERS_100,
        }
        if path == "/api/v1/players" and request.url.params.get("search") == "":
            return httpx.Response(422, json=ERROR_422)
        if path == "/api/v1/players" and request.url.params.get("search"):
            return httpx.Response(200, json=PLAYERS_SEARCH)
        if path in routes:
            return httpx.Response(200, json=routes[path])
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no mock route"}})

    results = run("http://afl-api.test", VALID_KEY, transport=httpx.MockTransport(handler))
    historical_check = next(r for r in results if "replay prerequisite" in r.name)
    assert historical_check.status == "SKIP"
    assert historical_check.required is True
    assert _not_fully_validated(results) == [historical_check]
