import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.lockouts import LockState, evaluate_match_lock
from app.replay import ReplayAflDataSource, ReplayEvidenceError
from app.replay_acquisition import acquire_first_half_2026, write_package
from scripts import first_half_replay


class Api:
    def __init__(self, *, no_roster=False, empty_stats=None, finality="final"):
        self.no_roster = no_roster
        self.empty_stats = empty_stats
        self.finality = finality
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
        if path == "/api/v1/seasons/712/players":
            return {
                "players": [
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
                        "eligible": True,
                    },
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
    payload = acquire_first_half_2026(
        Api(no_roster=True),
        source_base_url="https://user:secret@example.test:8443/private",
        acquired_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert payload["seasons"][0]["season_id"] == 712
    assert [r["round_number"] for r in payload["rounds"]] == list(range(10))
    assert len(payload["matches"]) == 10 and len(payload["player_stats"]) == 10
    assert payload["matches"][0]["provider_match_id"] == "m-100"
    assert {p["canonical_player_id"] for p in payload["players"]} == {44, 99}
    assert next(p for p in payload["players"] if p["canonical_player_id"] == 99)["identifiers"] == {"provider": "p99"}
    assert all(99 not in {row["canonical_player_id"] for row in rows} for rows in payload["player_stats"].values())
    assert payload["manifest"]["source_api"] == "https://example.test:8443"
    assert payload["manifest"]["roster_coverage"]["available"] == 0
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
