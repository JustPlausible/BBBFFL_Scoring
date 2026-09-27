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

from scripts.afl_contract_diagnostic import SEASON_PLAYERS_PAGE_LIMIT, run

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
SEASON_PLAYERS_85 = {
    "players": [
        {
            "canonical_player_id": 1,
            "display_name": "Test Player",
            "given_name": "Test",
            "family_name": "Player",
            "team": {"team_id": 1, "name": "Home"},
            "identifiers": {"afl_player_id": 100, "champion_data_player_id": "CD_I1"},
        },
        {
            "canonical_player_id": 2,
            "display_name": "Second Player",
            "given_name": "Second",
            "family_name": "Player",
            "team": {"team_id": 2, "name": "Away"},
            "identifiers": {"afl_player_id": 101, "champion_data_player_id": "CD_I2"},
        },
    ],
    "limit": SEASON_PLAYERS_PAGE_LIMIT,
    "offset": 0,
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
        "/api/v1/seasons/85/players": SEASON_PLAYERS_85,
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
    assert any(r.name == "GET /api/v1/seasons/{id}/players" and r.status == "PASS" for r in results)
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
            "/api/v1/seasons/85/players": SEASON_PLAYERS_85,
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


def test_diagnostic_falls_back_to_the_most_recent_season_when_none_is_flagged_current():
    """A real off-season snapshot of the live deployment (confirmed live for
    issue #244, the day after an AFL Grand Final): every season lists
    is_current=false at once. That is a genuine, expected timing state --
    identical to what AflApiClient.get_current_season would itself raise on
    right now -- not a reason for every downstream contract check to SKIP
    wholesale just because validation happened to run in the off-season."""
    off_season = copy.deepcopy(SEASONS)
    off_season["seasons"][0]["is_current"] = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != VALID_KEY:
            return httpx.Response(401, json=ERROR_401)
        path = request.url.path
        if path == "/api/v1/players/999999999999":
            return httpx.Response(404, json=ERROR_404)
        if path == "/api/v1/players" and request.url.params.get("search") == "":
            return httpx.Response(422, json=ERROR_422)
        if path == "/api/v1/players" and request.url.params.get("search"):
            return httpx.Response(200, json=PLAYERS_SEARCH)
        routes = {
            "/api/v1": DISCOVERY,
            "/api/v1/seasons": off_season,
            "/api/v1/seasons/85/rounds": ROUNDS_85,
            "/api/v1/seasons/84/rounds": ROUNDS_84,
            "/api/v1/seasons/85/players": SEASON_PLAYERS_85,
            "/api/v1/rounds/1/matches": MATCHES,
            "/api/v1/matches/100": MATCH_100,
            "/api/v1/matches/100/player-stats": PLAYER_STATS_100,
            "/api/v1/players/1": PLAYER_1,
            "/api/v1/players/1/seasons": PLAYER_1_SEASONS,
            "/api/v1/injuries": INJURIES,
            "/api/v1/matches/100/rosters": ROSTERS_100,
        }
        if path in routes:
            return httpx.Response(200, json=routes[path])
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no mock route"}})

    results = run("http://afl-api.test", VALID_KEY, transport=httpx.MockTransport(handler))

    seasons_check = next(r for r in results if r.name == "GET /api/v1/seasons")
    assert seasons_check.status == "FAIL"
    assert seasons_check.required is True
    assert "0 flagged is_current=true" in seasons_check.detail

    fallback_check = next(
        r for r in results if r.name == "seasons: an is_current season is resolved for downstream checks"
    )
    assert fallback_check.status == "SKIP"
    assert fallback_check.required is False
    assert "season_id=85" in fallback_check.detail

    for name in (
        "GET /api/v1/seasons/{id}/rounds",
        "GET /api/v1/seasons/{historical_id}/rounds (2026 replay prerequisite)",
        "GET /api/v1/seasons/{id}/players",
        "GET /api/v1/rounds/{id}/matches",
        "GET /api/v1/matches/{id}",
        "player-stats: lifecycle.finality is a recognised value",
        "GET /api/v1/players/{id}",
    ):
        check = next(r for r in results if r.name == name)
        assert check.status == "PASS", (name, check.detail)

    # The only required, not-fully-validated check is the honest signal
    # that no season is flagged is_current -- everything downstream still
    # ran against real fallback data instead of skipping wholesale.
    assert _not_fully_validated(results) == [seasons_check]


def test_diagnostic_flags_a_season_player_row_missing_a_required_field():
    broken = copy.deepcopy(SEASON_PLAYERS_85)
    del broken["players"][1]["team"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != VALID_KEY:
            return httpx.Response(401, json=ERROR_401)
        path = request.url.path
        if path == "/api/v1/seasons/85/players":
            return httpx.Response(200, json=broken)
        routes = {
            "/api/v1": DISCOVERY,
            "/api/v1/seasons": SEASONS,
            "/api/v1/seasons/85/rounds": ROUNDS_85,
            "/api/v1/seasons/84/rounds": ROUNDS_84,
        }
        if path in routes:
            return httpx.Response(200, json=routes[path])
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no mock route"}})

    results = run("http://afl-api.test", VALID_KEY, transport=httpx.MockTransport(handler))
    field_check = next(
        r for r in results if r.name == "season players: rows expose canonical_player_id/display_name/team/identifiers"
    )
    assert field_check.status == "FAIL"
    assert "2" in field_check.detail  # canonical_player_id of the broken second row
    assert "team" in field_check.detail


def test_diagnostic_follows_season_player_pagination_to_exhaustion():
    """The production client (`AflApiClient.get_season_players`) requests
    SEASON_PLAYERS_PAGE_LIMIT-sized pages and follows them to the
    terminating short page. This check must exercise the identical
    contract, not just a single small page (Codex review on PR #251) --
    otherwise a deployment that clamps the page size or malforms a later
    page would be reported compatible even though Season Setup would fail
    against it."""
    limit = SEASON_PLAYERS_PAGE_LIMIT
    page0_players = [
        {
            "canonical_player_id": i,
            "display_name": f"Player {i}",
            "given_name": f"Given{i}",
            "family_name": f"Family{i}",
            "team": {"team_id": 1, "name": "Home"},
            "identifiers": {"afl_player_id": 1000 + i, "champion_data_player_id": f"CD_I{i}"},
        }
        for i in range(limit)
    ]
    page1_players = [
        {
            "canonical_player_id": limit,
            "display_name": "Last Player",
            "given_name": "Last",
            "family_name": "Player",
            "team": {"team_id": 2, "name": "Away"},
            "identifiers": {"afl_player_id": 2000, "champion_data_player_id": "CD_ILAST"},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != VALID_KEY:
            return httpx.Response(401, json=ERROR_401)
        path = request.url.path
        if path == "/api/v1/seasons/85/players":
            offset = int(request.url.params.get("offset", "0"))
            if offset == 0:
                return httpx.Response(200, json={"players": page0_players, "limit": limit, "offset": 0})
            if offset == limit:
                return httpx.Response(200, json={"players": page1_players, "limit": limit, "offset": limit})
            return httpx.Response(200, json={"players": [], "limit": limit, "offset": offset})
        routes = {
            "/api/v1": DISCOVERY,
            "/api/v1/seasons": SEASONS,
            "/api/v1/seasons/85/rounds": ROUNDS_85,
            "/api/v1/seasons/84/rounds": ROUNDS_84,
        }
        if path in routes:
            return httpx.Response(200, json=routes[path])
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no mock route"}})

    results = run("http://afl-api.test", VALID_KEY, transport=httpx.MockTransport(handler))
    season_players_check = next(r for r in results if r.name == "GET /api/v1/seasons/{id}/players")
    assert season_players_check.status == "PASS", season_players_check.detail
    assert f"{limit + 1} players across 2 page(s)" in season_players_check.detail


def test_diagnostic_flags_a_repeated_canonical_player_id_across_season_player_pages():
    limit = SEASON_PLAYERS_PAGE_LIMIT
    page0_players = [
        {
            "canonical_player_id": i,
            "display_name": f"Player {i}",
            "given_name": None,
            "family_name": None,
            "team": {"team_id": 1, "name": "Home"},
            "identifiers": {},
        }
        for i in range(limit)
    ]
    # The first row of the "second page" repeats a canonical_player_id
    # already seen on the first page instead of the real deployment's next
    # player -- a genuine, deployment-side pagination defect that a
    # last-page-only check would never see.
    page1_players = [dict(page0_players[0])]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != VALID_KEY:
            return httpx.Response(401, json=ERROR_401)
        path = request.url.path
        if path == "/api/v1/seasons/85/players":
            offset = int(request.url.params.get("offset", "0"))
            if offset == 0:
                return httpx.Response(200, json={"players": page0_players, "limit": limit, "offset": 0})
            return httpx.Response(200, json={"players": page1_players, "limit": limit, "offset": limit})
        routes = {
            "/api/v1": DISCOVERY,
            "/api/v1/seasons": SEASONS,
            "/api/v1/seasons/85/rounds": ROUNDS_85,
            "/api/v1/seasons/84/rounds": ROUNDS_84,
        }
        if path in routes:
            return httpx.Response(200, json=routes[path])
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no mock route"}})

    results = run("http://afl-api.test", VALID_KEY, transport=httpx.MockTransport(handler))
    season_players_check = next(r for r in results if r.name == "GET /api/v1/seasons/{id}/players")
    assert season_players_check.status == "FAIL"
    assert "duplicates=" in season_players_check.detail


def test_diagnostic_flags_a_deployment_that_clamps_the_requested_page_size():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != VALID_KEY:
            return httpx.Response(401, json=ERROR_401)
        path = request.url.path
        if path == "/api/v1/seasons/85/players":
            # Ignores the requested limit (SEASON_PLAYERS_PAGE_LIMIT) and
            # always serves a smaller page -- AflApiClient.get_season_players
            # would treat this identically to a genuine short final page and
            # stop paginating early, silently under-populating the pool.
            return httpx.Response(200, json={"players": SEASON_PLAYERS_85["players"], "limit": 50, "offset": 0})
        routes = {
            "/api/v1": DISCOVERY,
            "/api/v1/seasons": SEASONS,
            "/api/v1/seasons/85/rounds": ROUNDS_85,
            "/api/v1/seasons/84/rounds": ROUNDS_84,
        }
        if path in routes:
            return httpx.Response(200, json=routes[path])
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no mock route"}})

    results = run("http://afl-api.test", VALID_KEY, transport=httpx.MockTransport(handler))
    season_players_check = next(r for r in results if r.name == "GET /api/v1/seasons/{id}/players")
    assert season_players_check.status == "FAIL"
    assert "echoed limit" in season_players_check.detail
