import json
from datetime import datetime, timezone

import pytest

from app.replay import ReplayAflDataSource, ReplayEvidenceError
from app.replay_acquisition import acquire_first_half_2026, write_package


class Api:
    def __init__(self, *, no_roster=False, empty_stats=None):
        self.no_roster = no_roster
        self.empty_stats = empty_stats
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path == "/api/v1/seasons":
            return {
                "seasons": [
                    {"season_id": 91, "year": 2025},
                    {"season_id": 712, "year": 2026, "current_round_number": 9},
                ]
            }
        if path == "/api/v1/seasons/712/rounds":
            return {
                "rounds": [
                    {"round_id": 100, "round_number": 0, "name": "Opening Round", "byes": []},
                    *[{"round_id": 100 + n, "round_number": n, "name": f"Round {n}", "byes": []} for n in range(1, 10)],
                ]
            }
        if "/rounds/" in path:
            rid = int(path.split("/")[4])
            return {
                "matches": [
                    {
                        "match_id": rid * 10,
                        "round_id": rid,
                        "status": "CONCLUDED",
                        "start_time_utc": "2026-03-01T08:00:00Z",
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
                        "display_name": "Player",
                        "team_id": 1,
                        "identifiers": {"provider": "p44"},
                        "stats": {"goals": 1, "behinds": 2, "disposals": 3, "marks": 4, "hitouts": 5, "tackles": 6},
                    }
                ]
            )
            return {"players": rows}
        if self.no_roster:
            raise RuntimeError("not captured")
        return {"selected": [44], "emergencies": [], "ins": [], "outs": []}


def test_acquisition_finds_year_and_exports_complete_identified_package(tmp_path):
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
    assert payload["players"][0]["identifiers"] == {"provider": "p44"}
    assert payload["manifest"]["source_api"] == "https://example.test:8443"
    assert payload["manifest"]["roster_coverage"]["available"] == 0
    path = tmp_path / "evidence.json"
    write_package(payload, path)
    state = tmp_path / "checkpoint.json"
    state.write_text(
        json.dumps(
            {"schema": "bbbffl.replay-checkpoint/v1", "effective_at": "2026-02-28T00:00:00Z", "stage": "scheduled"}
        )
    )
    assert ReplayAflDataSource(path, checkpoint_path=state).manifest["match_count"] == 10


def test_acquisition_missing_required_stats_fails_with_match_identity():
    with pytest.raises(ReplayEvidenceError, match="AFL match 1000"):
        acquire_first_half_2026(Api(empty_stats=1000), source_base_url="http://api")


def test_historical_scheduled_boundary_and_explicit_finality(tmp_path):
    payload = acquire_first_half_2026(
        Api(), source_base_url="http://api", acquired_at=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    evidence = tmp_path / "e.json"
    write_package(payload, evidence)
    state = tmp_path / "s.json"
    state.write_text(
        json.dumps(
            {"schema": "bbbffl.replay-checkpoint/v1", "effective_at": "2026-03-01T07:59:59Z", "stage": "scheduled"}
        )
    )
    before = ReplayAflDataSource(evidence, checkpoint_path=state)
    assert before.get_matches(100)[0].status == "UPCOMING"
    with pytest.raises(ReplayEvidenceError, match="before final-results"):
        before.get_match_player_stats(1000)
    state.write_text(
        json.dumps(
            {"schema": "bbbffl.replay-checkpoint/v1", "effective_at": "2026-03-01T08:00:00Z", "stage": "scheduled"}
        )
    )
    boundary = ReplayAflDataSource(evidence, checkpoint_path=state)
    assert boundary.clock.now().isoformat() == "2026-03-01T08:00:00+00:00"
    state.write_text(
        json.dumps(
            {"schema": "bbbffl.replay-checkpoint/v1", "effective_at": "2026-03-01T12:00:00Z", "stage": "final-results"}
        )
    )
    final = ReplayAflDataSource(evidence, checkpoint_path=state)
    assert final.get_matches(100)[0].status == "CONCLUDED"
    assert final.get_match_player_stats(1000)[44].goals == 1
