import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app.lockouts import LockState, evaluate_match_lock
from app.replay import ReplayAflDataSource, ReplayEvidenceError
from app.replay_acquisition import acquire_first_half_2026, write_json_pair_atomic, write_package
from scripts import first_half_replay

DEFAULT_PLAYERS = [
    {
        "canonical_player_id": 44,
        "display_name": "Participant",
        "team": {"team_id": 1, "name": "A"},
        "identifiers": {"provider": "p44"},
    },
    {
        "canonical_player_id": 99,
        "display_name": "Eligible non-participant",
        "team": {"team_id": 2, "name": "B"},
        "identifiers": {"provider": "p99"},
    },
]


def make_players(n, *, start=1):
    """Synthesize `n` valid season-player rows matching the AFL-api #248 contract."""
    return [
        {
            "canonical_player_id": pid,
            "display_name": f"Player {pid}",
            "team": {"team_id": 1 + (pid % 18), "name": f"Club {1 + (pid % 18)}"},
            "identifiers": {"afl_player_id": pid, "champion_data_id": None},
        }
        for pid in range(start, start + n)
    ]


class Api:
    """Fake consumer API modelling the real merged AFL-api #248 season-player
    envelope: {"players": [...], "limit": ..., "offset": ...}. Season-player
    pages are served by slicing `self.players` at the requested limit/offset,
    exactly like the real paginated collection."""

    def __init__(self, *, players=None, no_roster=False, empty_stats=None, finality="final"):
        self.no_roster = no_roster
        self.empty_stats = empty_stats
        self.finality = finality
        self.calls = []
        self.players = DEFAULT_PLAYERS if players is None else players

    def get(self, path):
        self.calls.append(path)
        if path == "/api/v1/seasons":
            return {
                "seasons": [
                    {"season_id": 91, "year": 2025},
                    {"season_id": 712, "year": 2026, "current_round_number": 9},
                ]
            }
        if path.startswith("/api/v1/seasons/712/players"):
            return self._players_page(path)
        if path == "/api/v1/seasons/712/rounds":
            return {
                "rounds": [
                    {"round_id": 100, "round_number": 0, "name": "Opening Round", "byes": []},
                    *[{"round_id": 100 + n, "round_number": n, "name": f"Round {n}", "byes": []} for n in range(1, 10)],
                ]
            }
        if "/rounds/" in path:
            rid = int(path.split("/")[4])
            day = rid - 99
            return {
                "matches": [
                    {
                        "match_id": rid * 10,
                        "round_id": rid,
                        "status": "CONCLUDED",
                        "start_time_utc": f"2026-03-{day:02d}T08:00:00Z",
                        "home_team": {"team_id": 1, "name": "A"},
                        "away_team": {"team_id": 2, "name": "B"},
                        "provider_match_id": f"m-{rid}",
                    }
                ]
            }
        mid = int(path.split("/")[4])
        if path.endswith("player-stats"):
            rows = (
                []
                if self.empty_stats == mid
                else [
                    {
                        "canonical_player_id": 44,
                        "display_name": "Participant",
                        "team_id": 1,
                        "identifiers": {"provider": "p44"},
                        "stats": {"goals": 1, "behinds": 2, "disposals": 3, "marks": 4, "hitouts": 5, "tackles": 6},
                    }
                ]
            )
            return {"lifecycle": {"finality": self.finality}, "players": rows}
        if self.no_roster:
            raise RuntimeError("not captured")
        return {"selected": [44], "emergencies": [], "ins": [], "outs": []}

    def _players_page(self, path):
        query = parse_qs(urlsplit(path).query)
        limit = int(query.get("limit", ["250"])[0])
        offset = int(query.get("offset", ["0"])[0])
        page = self.players[offset : offset + limit]
        return {"players": page, "limit": limit, "offset": offset}


def checkpoint(path, effective_at, finalised=(), stage="scheduled"):
    path.write_text(
        json.dumps(
            {
                "schema": "bbbffl.replay-checkpoint/v1",
                "effective_at": effective_at,
                "stage": stage,
                "finalised_round_ids": list(finalised),
            }
        )
    )


def test_acquisition_uses_authoritative_pool_and_exports_complete_identified_package(tmp_path):
    api = Api(no_roster=True)
    payload = acquire_first_half_2026(
        api,
        source_base_url="https://user:secret@example.test:8443/private",
        acquired_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert payload["seasons"][0]["season_id"] == 712
    assert [r["round_number"] for r in payload["rounds"]] == list(range(10))
    assert len(payload["matches"]) == 10 and len(payload["player_stats"]) == 10
    assert payload["matches"][0]["provider_match_id"] == "m-100"
    assert {p["canonical_player_id"] for p in payload["players"]} == {44, 99}
    assert next(p for p in payload["players"] if p["canonical_player_id"] == 99)["identifiers"] == {"provider": "p99"}
    assert all("eligible" not in p for p in payload["players"])
    assert all(99 not in {row["canonical_player_id"] for row in rows} for rows in payload["player_stats"].values())
    assert payload["manifest"]["source_api"] == "https://example.test:8443"
    assert payload["manifest"]["roster_coverage"]["available"] == 0
    assert payload["manifest"]["player_pool_count"] == 2
    assert payload["manifest"]["player_pool_page_count"] == 1
    assert [c for c in api.calls if c.startswith("/api/v1/seasons/712/players")] == [
        "/api/v1/seasons/712/players?limit=250&offset=0"
    ]
    path = tmp_path / "evidence.json"
    write_package(payload, path)
    state = tmp_path / "checkpoint.json"
    checkpoint(state, "2026-02-28T00:00:00Z")
    assert ReplayAflDataSource(path, checkpoint_path=state).manifest["match_count"] == 10


def test_acquisition_missing_required_stats_fails_with_match_identity():
    with pytest.raises(ReplayEvidenceError, match="AFL match 1000"):
        acquire_first_half_2026(Api(empty_stats=1000), source_base_url="http://api")


@pytest.mark.parametrize("finality", ["partial", "not_available", None, "unknown"])
def test_acquisition_rejects_every_non_final_stats_response(finality):
    with pytest.raises(ReplayEvidenceError, match=rf"match 1000.*finality={finality!r}"):
        acquire_first_half_2026(Api(finality=finality), source_base_url="http://api")


def test_final_stats_response_is_accepted():
    assert len(acquire_first_half_2026(Api(finality="final"), source_base_url="http://api")["player_stats"]) == 10


def test_multi_round_finality_is_scoped_monotonic_and_persisted(tmp_path):
    payload = acquire_first_half_2026(
        Api(), source_base_url="http://api", acquired_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    evidence = tmp_path / "e.json"
    write_package(payload, evidence)
    state = tmp_path / "s.json"

    checkpoint(state, "2026-03-01T07:59:59Z")
    before = ReplayAflDataSource(evidence, checkpoint_path=state)
    assert before.get_matches(100)[0].status == before.get_matches(101)[0].status == "UPCOMING"
    with pytest.raises(ReplayEvidenceError):
        before.get_match_player_stats(1000)
    with pytest.raises(ReplayEvidenceError):
        before.get_match_player_stats(1010)

    checkpoint(state, "2026-03-01T08:00:00Z")
    started = ReplayAflDataSource(evidence, checkpoint_path=state)
    assert evaluate_match_lock(started.get_matches(100)[0], started.clock.now())[0] is LockState.LOCKED
    assert evaluate_match_lock(started.get_matches(101)[0], started.clock.now())[0] is LockState.EDITABLE
    with pytest.raises(ReplayEvidenceError):
        started.get_match_player_stats(1000)

    checkpoint(state, "2026-03-01T12:00:00Z", [100], "final-results")
    round_one_final = ReplayAflDataSource(evidence, checkpoint_path=state)
    assert round_one_final.get_matches(100)[0].status == "CONCLUDED"
    assert round_one_final.get_match_player_stats(1000)[44].goals == 1
    assert round_one_final.get_matches(101)[0].status == "UPCOMING"
    with pytest.raises(ReplayEvidenceError):
        round_one_final.get_match_player_stats(1010)

    checkpoint(state, "2026-03-02T08:00:00Z", [100])
    round_two_started = ReplayAflDataSource(evidence, checkpoint_path=state)
    assert round_two_started.get_matches(100)[0].status == "CONCLUDED"
    assert round_two_started.get_match_player_stats(1000)[44].goals == 1
    assert (
        evaluate_match_lock(round_two_started.get_matches(101)[0], round_two_started.clock.now())[0] is LockState.LOCKED
    )
    with pytest.raises(ReplayEvidenceError):
        round_two_started.get_match_player_stats(1010)

    checkpoint(state, "2026-03-02T12:00:00Z", [100, 101], "final-results")
    restarted = ReplayAflDataSource(evidence, checkpoint_path=state)
    assert [restarted.get_matches(rid)[0].status for rid in (100, 101)] == ["CONCLUDED", "CONCLUDED"]
    assert restarted.get_match_player_stats(1000)[44].goals == restarted.get_match_player_stats(1010)[44].goals == 1


def test_checkpoint_command_preserves_released_rounds_and_refuses_rewind(tmp_path, monkeypatch):
    state = tmp_path / "checkpoint.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "first_half_replay",
            "checkpoint",
            "--state",
            str(state),
            "--effective-at",
            "2026-03-01T12:00:00Z",
            "--stage",
            "final-results",
            "--round-id",
            "100",
        ],
    )
    assert first_half_replay.main() == 0
    monkeypatch.setattr(
        "sys.argv",
        [
            "first_half_replay",
            "checkpoint",
            "--state",
            str(state),
            "--effective-at",
            "2026-03-02T07:00:00Z",
            "--stage",
            "scheduled",
        ],
    )
    assert first_half_replay.main() == 0
    assert json.loads(state.read_text())["finalised_round_ids"] == [100]
    monkeypatch.setattr(
        "sys.argv", ["first_half_replay", "checkpoint", "--state", str(state), "--effective-at", "2026-03-01T00:00:00Z"]
    )
    assert first_half_replay.main() == 1


def test_playbook_source_mounts_scripts_for_one_off_compose_commands():
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "bbbffl_app/Dockerfile").read_text()
    playbook = (root / "docs/2026-first-half-replay-playbook.md").read_text()
    assert "COPY scripts" not in dockerfile
    source_mount = '$COMPOSE run --rm -v "$PWD/bbbffl_app:/app"'
    assert playbook.count(source_mount) >= 4
    assert "python -m scripts.bootstrap_2026_first_half" in playbook
    assert '-v "$PWD/replay/2026-first-half/state:/replay/state"' in playbook


# --- Season-player pagination ---------------------------------------------


def test_pagination_follows_every_page_of_a_large_season():
    api = Api(players=make_players(900))
    payload = acquire_first_half_2026(api, source_base_url="http://api")
    player_calls = [c for c in api.calls if c.startswith("/api/v1/seasons/712/players")]
    assert player_calls == [
        "/api/v1/seasons/712/players?limit=250&offset=0",
        "/api/v1/seasons/712/players?limit=250&offset=250",
        "/api/v1/seasons/712/players?limit=250&offset=500",
        "/api/v1/seasons/712/players?limit=250&offset=750",
    ]
    ids = [p["canonical_player_id"] for p in payload["players"]]
    assert len(ids) == 900
    assert ids == sorted(ids)
    assert payload["manifest"]["player_pool_count"] == 900
    assert payload["manifest"]["player_pool_page_count"] == 4


def test_pagination_terminal_partial_page_stops_requesting_more_pages():
    api = Api(players=make_players(260))
    payload = acquire_first_half_2026(api, source_base_url="http://api")
    player_calls = [c for c in api.calls if c.startswith("/api/v1/seasons/712/players")]
    assert player_calls == [
        "/api/v1/seasons/712/players?limit=250&offset=0",
        "/api/v1/seasons/712/players?limit=250&offset=250",
    ]
    assert len(payload["players"]) == 260


def test_pagination_empty_terminal_page_after_exact_multiple_of_limit_is_handled():
    api = Api(players=make_players(500))
    payload = acquire_first_half_2026(api, source_base_url="http://api")
    player_calls = [c for c in api.calls if c.startswith("/api/v1/seasons/712/players")]
    assert player_calls == [
        "/api/v1/seasons/712/players?limit=250&offset=0",
        "/api/v1/seasons/712/players?limit=250&offset=250",
        "/api/v1/seasons/712/players?limit=250&offset=500",
    ]
    assert len(payload["players"]) == 500


# --- Season-player pagination corruption -----------------------------------


class RepeatingContentApi(Api):
    """Broken upstream pagination: every page honestly echoes the requested
    offset in its envelope, but always serves the same first-page content --
    a real-world "offset parameter accepted but ignored" bug."""

    def _players_page(self, path):
        query = parse_qs(urlsplit(path).query)
        limit = int(query.get("limit", ["250"])[0])
        offset = int(query.get("offset", ["0"])[0])
        page = self.players[:limit]
        return {"players": page, "limit": limit, "offset": offset}


def test_repeated_overlapping_pages_fail_via_duplicate_detection():
    api = RepeatingContentApi(players=make_players(300))
    with pytest.raises(ReplayEvidenceError, match="duplicate canonical player"):
        acquire_first_half_2026(api, source_base_url="http://api")


class StuckOffsetApi(Api):
    """Broken upstream pagination: the collection never advances -- every
    page truthfully reports offset=0 no matter what was requested."""

    def _players_page(self, path):
        page = self.players[:250]
        return {"players": page, "limit": 250, "offset": 0}


def test_non_advancing_offset_fails_closed_instead_of_looping_or_truncating():
    api = StuckOffsetApi(players=make_players(300))
    with pytest.raises(ReplayEvidenceError, match="reports offset 0.*expected 250"):
        acquire_first_half_2026(api, source_base_url="http://api")
    # The paginator must not have spun indefinitely against the broken API.
    player_calls = [c for c in api.calls if c.startswith("/api/v1/seasons/712/players")]
    assert len(player_calls) == 2


class ClampedLimitApi(Api):
    """Broken/incompatible upstream pagination: a deployment-side clamp
    serves fewer rows per page than requested, but the envelope honestly
    reports the clamped limit -- this must not be mistaken for a genuine
    short/terminal page, or the pool silently truncates."""

    CLAMPED_LIMIT = 100

    def _players_page(self, path):
        query = parse_qs(urlsplit(path).query)
        offset = int(query.get("offset", ["0"])[0])
        page = self.players[offset : offset + self.CLAMPED_LIMIT]
        return {"players": page, "limit": self.CLAMPED_LIMIT, "offset": offset}


def test_clamped_page_limit_fails_closed_instead_of_looking_terminal():
    api = ClampedLimitApi(players=make_players(300))
    with pytest.raises(ReplayEvidenceError, match="reports limit 100, expected 250"):
        acquire_first_half_2026(api, source_base_url="http://api")
    # Must fail on the very first page rather than silently accepting a
    # short/clamped page as the valid final page.
    player_calls = [c for c in api.calls if c.startswith("/api/v1/seasons/712/players")]
    assert len(player_calls) == 1


def test_malformed_players_envelope_fails_closed():
    class NotAnObjectApi(Api):
        def _players_page(self, path):
            return ["not", "an", "object"]

    with pytest.raises(ReplayEvidenceError, match="expected an object envelope"):
        acquire_first_half_2026(NotAnObjectApi(), source_base_url="http://api")


def test_missing_players_list_fails_closed():
    class NoPlayersKeyApi(Api):
        def _players_page(self, path):
            return {"limit": 250, "offset": 0}

    with pytest.raises(ReplayEvidenceError, match="expected a players list"):
        acquire_first_half_2026(NoPlayersKeyApi(), source_base_url="http://api")


# --- Season-player identity contract ---------------------------------------

BAD_PLAYER_CASES = [
    (
        "non_integer_canonical_id",
        {"canonical_player_id": "44", "display_name": "X", "team": {"team_id": 1, "name": "A"}},
        "malformed canonical_player_id",
    ),
    (
        "zero_canonical_id",
        {"canonical_player_id": 0, "display_name": "X", "team": {"team_id": 1, "name": "A"}},
        "malformed canonical_player_id",
    ),
    (
        "negative_canonical_id",
        {"canonical_player_id": -5, "display_name": "X", "team": {"team_id": 1, "name": "A"}},
        "malformed canonical_player_id",
    ),
    (
        "missing_display_name",
        {"canonical_player_id": 44, "team": {"team_id": 1, "name": "A"}},
        "blank or missing display_name",
    ),
    (
        "blank_display_name",
        {"canonical_player_id": 44, "display_name": "   ", "team": {"team_id": 1, "name": "A"}},
        "blank or missing display_name",
    ),
    (
        "null_team",
        {"canonical_player_id": 44, "display_name": "X", "team": None},
        "no resolved requested-season team",
    ),
    (
        "missing_team",
        {"canonical_player_id": 44, "display_name": "X"},
        "no resolved requested-season team",
    ),
    (
        "current_team_does_not_rescue_missing_team",
        {"canonical_player_id": 44, "display_name": "X", "current_team": {"team_id": 1, "name": "A"}},
        "no resolved requested-season team",
    ),
    (
        "zero_team_id",
        {"canonical_player_id": 44, "display_name": "X", "team": {"team_id": 0, "name": "A"}},
        "malformed team.team_id",
    ),
    (
        "non_integer_team_id",
        {"canonical_player_id": 44, "display_name": "X", "team": {"team_id": "1", "name": "A"}},
        "malformed team.team_id",
    ),
    (
        "blank_team_name",
        {"canonical_player_id": 44, "display_name": "X", "team": {"team_id": 1, "name": "  "}},
        "blank or missing team.name",
    ),
    (
        "missing_team_name",
        {"canonical_player_id": 44, "display_name": "X", "team": {"team_id": 1}},
        "blank or missing team.name",
    ),
]


@pytest.mark.parametrize("name,row,match", BAD_PLAYER_CASES, ids=[case[0] for case in BAD_PLAYER_CASES])
def test_malformed_season_player_row_fails_closed(name, row, match):
    with pytest.raises(ReplayEvidenceError, match=match):
        acquire_first_half_2026(Api(players=[row]), source_base_url="http://api")


# --- Provider identifiers ----------------------------------------------------


def test_provider_identifiers_may_independently_be_null_and_are_preserved_verbatim():
    row = {
        "canonical_player_id": 44,
        "display_name": "Participant",
        "team": {"team_id": 1, "name": "A"},
        "identifiers": {"afl_player_id": None, "champion_data_id": 555},
    }
    payload = acquire_first_half_2026(Api(players=[row]), source_base_url="http://api")
    assert payload["players"][0]["identifiers"] == {"afl_player_id": None, "champion_data_id": 555}


def test_both_provider_identifiers_may_be_null():
    row = {
        "canonical_player_id": 44,
        "display_name": "Participant",
        "team": {"team_id": 1, "name": "A"},
        "identifiers": {"afl_player_id": None, "champion_data_id": None},
    }
    payload = acquire_first_half_2026(Api(players=[row]), source_base_url="http://api")
    assert payload["players"][0]["identifiers"] == {"afl_player_id": None, "champion_data_id": None}


# --- Preseason/current-season membership behaviour --------------------------


def test_season_member_without_match_appearance_stays_in_pool_and_afl_eligible_field_is_ignored():
    rows = [
        DEFAULT_PLAYERS[0],
        {**DEFAULT_PLAYERS[1], "eligible": False},  # AFL-api has no eligible field; any value must be ignored
    ]
    api = Api(players=rows, no_roster=True)
    payload = acquire_first_half_2026(api, source_base_url="http://api")
    assert {p["canonical_player_id"] for p in payload["players"]} == {44, 99}
    assert all("eligible" not in p for p in payload["players"])
    assert not any("summar" in call.lower() or "statspro" in call.lower() for call in api.calls)


def test_historical_stat_player_absent_from_season_membership_fails_closed():
    rows = [{"canonical_player_id": 50, "display_name": "Other", "team": {"team_id": 1, "name": "A"}}]
    with pytest.raises(ReplayEvidenceError, match="missing from season 712 player pool"):
        acquire_first_half_2026(Api(players=rows), source_base_url="http://api")


# --- Failure-safe CLI output --------------------------------------------------


def _fake_afl_api_client_factory(api):
    class FakeAflApiClient:
        def __init__(self, base_url, api_key):
            self.base_url = base_url
            self.api_key = api_key

        def _get(self, path):
            return api.get(path)

        def close(self):
            pass

    return FakeAflApiClient


def test_failed_acquisition_does_not_report_pass_or_write_partial_output(tmp_path, monkeypatch, capsys):
    output = tmp_path / "evidence.json"
    pool_output = tmp_path / "pool.json"
    output.write_text("PREVIOUS-GOOD-EVIDENCE")
    pool_output.write_text("PREVIOUS-GOOD-POOL")
    monkeypatch.setattr(first_half_replay, "AflApiClient", _fake_afl_api_client_factory(Api(finality="not_available")))
    monkeypatch.setattr(
        "sys.argv",
        [
            "first_half_replay",
            "acquire",
            "--output",
            str(output),
            "--player-pool-output",
            str(pool_output),
            "--base-url",
            "http://api",
        ],
    )
    assert first_half_replay.main() == 1
    captured = capsys.readouterr()
    assert "PASS" not in captured.out
    assert output.read_text() == "PREVIOUS-GOOD-EVIDENCE"
    assert pool_output.read_text() == "PREVIOUS-GOOD-POOL"


def test_successful_acquisition_writes_bootstrap_compatible_player_pool(tmp_path, monkeypatch, capsys):
    output = tmp_path / "evidence.json"
    pool_output = tmp_path / "pool.json"
    monkeypatch.setattr(first_half_replay, "AflApiClient", _fake_afl_api_client_factory(Api(no_roster=True)))
    monkeypatch.setattr(
        "sys.argv",
        [
            "first_half_replay",
            "acquire",
            "--output",
            str(output),
            "--player-pool-output",
            str(pool_output),
            "--base-url",
            "http://api",
        ],
    )
    assert first_half_replay.main() == 0
    captured = capsys.readouterr()
    assert "acquisition PASS" in captured.out
    pool = json.loads(pool_output.read_text())
    assert pool["source"] == {"provider": "afl-api-v1", "season_year": 2026}
    assert {p["canonical_player_id"] for p in pool["players"]} == {44, 99}
    assert all(p["eligible"] is True for p in pool["players"])


def test_write_json_pair_atomic_stages_every_output_before_replacing_any(tmp_path):
    good_target = tmp_path / "evidence.json"
    good_target.write_text("PREVIOUS-GOOD-EVIDENCE")
    # Force staging the second item to fail: its parent path is a plain file,
    # not a directory, so `target.parent.mkdir(parents=True, exist_ok=True)`
    # raises before any write for that item happens.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    bad_target = blocker / "pool.json"

    with pytest.raises(OSError):
        write_json_pair_atomic([({"a": 1}, good_target), ({"b": 2}, bad_target)])

    # The first item must not have been replaced just because it was staged
    # before the second item's failure was discovered.
    assert good_target.read_text() == "PREVIOUS-GOOD-EVIDENCE"


def test_write_json_pair_atomic_rejects_aliased_output_destinations(tmp_path):
    good_target = tmp_path / "evidence.json"
    good_target.write_text("PREVIOUS-GOOD-EVIDENCE")
    same_target_again = tmp_path / "evidence.json"

    with pytest.raises(ValueError, match="distinct output paths"):
        write_json_pair_atomic([({"a": 1}, good_target), ({"b": 2}, same_target_again)])

    # Rejected before any staging/replace happens -- the previously
    # completed, known-good output must be left untouched, not clobbered
    # with whichever of the two aliased payloads staged last.
    assert good_target.read_text() == "PREVIOUS-GOOD-EVIDENCE"


def test_write_json_pair_atomic_rejects_symlink_aliased_destinations(tmp_path):
    good_target = tmp_path / "evidence.json"
    good_target.write_text("PREVIOUS-GOOD-EVIDENCE")
    alias = tmp_path / "evidence-alias.json"
    alias.symlink_to(good_target)

    with pytest.raises(ValueError, match="distinct output paths"):
        write_json_pair_atomic([({"a": 1}, good_target), ({"b": 2}, alias)])

    assert good_target.read_text() == "PREVIOUS-GOOD-EVIDENCE"


def test_write_json_pair_atomic_rejects_directory_as_output_target(tmp_path):
    good_target = tmp_path / "evidence.json"
    good_target.write_text("PREVIOUS-GOOD-EVIDENCE")
    # A pre-existing directory at the second target would make its
    # replace() raise mid-loop, after the first target has already been
    # replaced -- reject it up front instead, before either target is
    # touched.
    directory_target = tmp_path / "pool_dir"
    directory_target.mkdir()

    with pytest.raises(ValueError, match="must be regular files"):
        write_json_pair_atomic([({"a": 1}, good_target), ({"b": 2}, directory_target)])

    assert good_target.read_text() == "PREVIOUS-GOOD-EVIDENCE"


def test_write_json_pair_atomic_rejects_temp_path_colliding_with_another_target(tmp_path, monkeypatch):
    import app.replay_acquisition as replay_acquisition

    # A deterministic `.tmp`-suffix temp name derived from one target could
    # collide with another item's *actual* requested target path (e.g.
    # `--output pool.json.tmp --player-pool-output pool.json`). Force that
    # collision deterministically by pinning the random component.
    fixed_hex = "deadbeefdeadbeefdeadbeefdeadbeef"
    monkeypatch.setattr(replay_acquisition, "uuid4", lambda: SimpleNamespace(hex=fixed_hex))

    target_a = tmp_path / "evidence.json"
    target_b = tmp_path / f"evidence.json.{fixed_hex}.tmp"

    with pytest.raises(ValueError, match="temp path collides with an output target"):
        write_json_pair_atomic([({"a": 1}, target_a), ({"b": 2}, target_b)])
