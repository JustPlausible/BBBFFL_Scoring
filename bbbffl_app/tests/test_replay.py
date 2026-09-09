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
    assert source.get_match_player_stats(2601)[66001].disposals == 16
    assert {record["evidence_class"] for record in source.evidence_records()} >= {
        EvidenceClass.KNOWN_FACT.value,
        EvidenceClass.RECONSTRUCTABLE_BEHAVIOUR.value,
        EvidenceClass.SYNTHETIC_SCENARIO.value,
    }


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
    payload = json.loads(FIXTURE.read_text())
    del payload["players"][0]["provenance"]
    unclassified = tmp_path / "unclassified.json"
    unclassified.write_text(json.dumps(payload))
    with pytest.raises(ReplayEvidenceError, match="provenance.source"):
        ReplayAflDataSource(unclassified)


def test_duplicate_identities_in_raw_evidence_fail_closed(tmp_path):
    """A second raw record sharing an identity with an existing one must be
    rejected before the section is collapsed into an identity-keyed dict --
    otherwise it would silently overwrite the first record with no trace
    (issue #174 Codex review: `ReplayAflDataSource._load` previously let two
    `matches` rows share one `match_id`, invisible once loaded)."""
    base = json.loads(FIXTURE.read_text())

    def write(payload):
        path = tmp_path / "dup.json"
        path.write_text(json.dumps(payload))
        return path

    payload = json.loads(json.dumps(base))
    payload["players"].append(dict(payload["players"][0]))
    with pytest.raises(ReplayEvidenceError, match="duplicate player identity"):
        ReplayAflDataSource(write(payload))

    payload = json.loads(json.dumps(base))
    payload["matches"].append(dict(payload["matches"][0]))
    with pytest.raises(ReplayEvidenceError, match="duplicate match identity"):
        ReplayAflDataSource(write(payload))

    payload = json.loads(json.dumps(base))
    payload["rounds"].append(dict(payload["rounds"][0]))
    with pytest.raises(ReplayEvidenceError, match="duplicate round identity"):
        ReplayAflDataSource(write(payload))

    payload = json.loads(json.dumps(base))
    payload["seasons"].append(dict(payload["seasons"][0]))
    with pytest.raises(ReplayEvidenceError, match="duplicate season identity"):
        ReplayAflDataSource(write(payload))

    payload = json.loads(json.dumps(base))
    payload["player_stats"]["2601"].append(dict(payload["player_stats"]["2601"][0]))
    with pytest.raises(ReplayEvidenceError, match="duplicate player_stat identity"):
        ReplayAflDataSource(write(payload))


def test_duplicate_identity_detection_normalizes_int_and_string_forms(tmp_path):
    """A duplicate whose second record spells the same identity as a string
    (`"66001"` vs `66001`) must still be caught -- both forms collapse to
    the same key once the dict comprehensions below apply `int(...)`, so
    comparing the raw, un-normalized values would miss exactly this
    collision (issue #174 Codex review, second round)."""
    base = json.loads(FIXTURE.read_text())

    def write(payload):
        path = tmp_path / "dup-string-id.json"
        path.write_text(json.dumps(payload))
        return path

    payload = json.loads(json.dumps(base))
    duplicate_player = dict(payload["players"][0])
    duplicate_player["canonical_player_id"] = str(duplicate_player["canonical_player_id"])
    payload["players"].append(duplicate_player)
    with pytest.raises(ReplayEvidenceError, match="duplicate player identity"):
        ReplayAflDataSource(write(payload))

    payload = json.loads(json.dumps(base))
    duplicate_match = dict(payload["matches"][0])
    duplicate_match["match_id"] = str(duplicate_match["match_id"])
    payload["matches"].append(duplicate_match)
    with pytest.raises(ReplayEvidenceError, match="duplicate match identity"):
        ReplayAflDataSource(write(payload))


def test_match_lifecycle_is_selected_by_replay_clock_from_same_evidence():
    before = ReplayAflDataSource(FIXTURE, clock=ReplayClock.from_iso("2026-03-19T08:29:00Z"))
    during = ReplayAflDataSource(FIXTURE, clock=ReplayClock.from_iso("2026-03-19T08:31:00Z"))
    final = ReplayAflDataSource(FIXTURE, clock=ReplayClock.from_iso("2026-03-19T12:01:00Z"))
    assert [source.get_matches(1344)[0].status for source in (before, during, final)] == [
        "UPCOMING",
        "LIVE",
        "CONCLUDED",
    ]


def test_replay_clock_is_explicit_timezone_aware_and_stable():
    clock = ReplayClock.from_iso("2026-03-19T08:29:00Z")
    assert clock.now() == datetime(2026, 3, 19, 8, 29, tzinfo=timezone.utc)
    assert clock.now() == clock.now()
    with pytest.raises(ValueError, match="timezone-aware"):
        ReplayClock(datetime(2026, 3, 19))


def test_reports_require_all_domain_sections_and_are_deterministic(tmp_path):
    report = {
        "run": {"run_id": "r1", "season": 2026, "round": 1, "evidence_manifest": "m", "evidence_version": "1"},
        "mapping": {},
        "lineups": [],
        "lockout": {},
        "scoring": [],
        "scorer_workflow": {},
        "official_results": [],
        "ladder": [],
        "discrepancies": [],
    }
    first, second, summary = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "summary.txt"
    write_replay_report(report, first, summary)
    write_replay_report(report, second, summary)
    assert first.read_bytes() == second.read_bytes()
    assert "Discrepancies: 0" in summary.read_text()
    with pytest.raises(ReplayEvidenceError, match="incomplete"):
        write_replay_report({"run": report["run"]}, first, summary)
