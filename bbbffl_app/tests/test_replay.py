import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.replay import EvidenceClass, ReplayAflDataSource, ReplayClock, ReplayEvidenceError, write_replay_report

FIXTURE = Path(__file__).parent / "fixtures" / "replay_round_2026" / "evidence.json"


def test_controlled_evidence_loads_and_retains_manifest_provenance():
    source = ReplayAflDataSource(FIXTURE)
    assert source.manifest["evidence_class"] == EvidenceClass.SYNTHETIC_SCENARIO.value
    assert source.get_round(2026, 1).round_id == 1344
    assert source.get_matches(1344)[0].start_time_utc == "2026-03-19T08:30:00Z"
    assert source.get_match_player_stats(2601)[66001].disposals == 20


def test_missing_malformed_and_incomplete_evidence_fail_closed(tmp_path):
    with pytest.raises(ReplayEvidenceError, match="does not exist"):
        ReplayAflDataSource(tmp_path / "absent.json")
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{")
    with pytest.raises(ReplayEvidenceError, match="malformed"):
        ReplayAflDataSource(malformed)
    payload = json.loads(FIXTURE.read_text())
    del payload["player_stats"]["2601"]
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps(payload))
    with pytest.raises(ReplayEvidenceError, match="player_stats"):
        ReplayAflDataSource(incomplete)


def test_replay_clock_is_explicit_timezone_aware_and_stable():
    clock = ReplayClock.from_iso("2026-03-19T08:29:00Z")
    assert clock.now() == datetime(2026, 3, 19, 8, 29, tzinfo=timezone.utc)
    assert clock.now() == clock.now()
    with pytest.raises(ValueError, match="timezone-aware"):
        ReplayClock(datetime(2026, 3, 19))


def test_reports_require_all_domain_sections_and_are_deterministic(tmp_path):
    report = {
        "run": {"run_id": "r1", "season": 2026, "round": 1, "evidence_manifest": "m", "evidence_version": "1"},
        "mapping": {}, "lineups": [], "lockout": {}, "scoring": [], "scorer_workflow": {},
        "official_results": [], "ladder": [], "discrepancies": [],
    }
    first, second, summary = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "summary.txt"
    write_replay_report(report, first, summary)
    write_replay_report(report, second, summary)
    assert first.read_bytes() == second.read_bytes()
    assert "Discrepancies: 0" in summary.read_text()
    with pytest.raises(ReplayEvidenceError, match="incomplete"):
        write_replay_report({"run": report["run"]}, first, summary)
