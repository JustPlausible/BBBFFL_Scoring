"""Tests for the AFL evidence fixture loader/validator itself (issue #40 /
roadmap package 08) -- deterministic loading, provenance availability,
evidence classification, malformed-fixture rejection, secret-scanning, and
that the whole committed corpus is clean.

`tests/test_lockouts.py` and `tests/test_calculations.py` separately prove
this evidence is actually useful to BBBFFL domain behaviour (a lockout and
a scoring scenario respectively) -- this file only tests the
loading/validation machinery in isolation.
"""

import json

import httpx
import pytest

from tests import afl_evidence
from tests.afl_evidence import EvidenceNotFoundError, EvidenceValidationError, Provenance

# --- Deterministic loading, offline by construction ------------------------


def test_load_a_known_fixture_deterministically():
    first = afl_evidence.load("v1/synthetic/season_85/round_1500/matches.json")
    second = afl_evidence.load("v1/synthetic/season_85/round_1500/matches.json")
    assert first.response == second.response
    assert first.provenance == second.provenance


def test_missing_fixture_raises_a_clear_not_found_error():
    with pytest.raises(EvidenceNotFoundError, match="no evidence fixture"):
        afl_evidence.load("v1/synthetic/season_85/does_not_exist.json")


# --- Provenance availability ------------------------------------------------


def test_provenance_is_available_and_typed():
    fixture = afl_evidence.load("v1/synthetic/season_85/round_1500/match_9503/player_stats.json")
    assert isinstance(fixture.provenance, Provenance)
    assert fixture.provenance.season_id == 85
    assert fixture.provenance.round_id == 1500
    assert fixture.provenance.match_id == 9503
    assert 9701 in fixture.provenance.canonical_player_ids
    assert fixture.provenance.contract_version == "v1"
    assert fixture.provenance.endpoint == "GET /api/v1/matches/{match_id}/player-stats"
    assert fixture.provenance.notes  # a real explanation, not blank


def test_provenance_survives_being_read_from_a_copied_file(tmp_path):
    """Provenance is embedded in the fixture file itself, not a side-car
    index, so copying the file elsewhere (simulating a later
    reorganisation) keeps it intact and still loadable."""
    original = afl_evidence.FIXTURES_ROOT / "v1/synthetic/season_85/round_1500/matches.json"
    copy_root = tmp_path / "afl_evidence"
    copy_path = copy_root / "v1" / "synthetic" / "season_85" / "round_1500" / "matches.json"
    copy_path.parent.mkdir(parents=True)
    copy_path.write_text(original.read_text())

    envelope = json.loads(copy_path.read_text())
    assert envelope["provenance"]["season_id"] == 85
    assert envelope["provenance"]["round_id"] == 1500
    assert envelope["provenance"]["notes"]


# --- Evidence classification -------------------------------------------------


def test_every_classification_is_programmatically_determined_not_inferred_from_filename():
    synthetic = afl_evidence.load("v1/synthetic/season_85/season.json")
    unresolved = afl_evidence.load("v1/unresolved/season_85/round_1500/match_9503/participation_9706.json")
    assert synthetic.provenance.classification == "synthetic"
    assert unresolved.provenance.classification == "unresolved"
    assert unresolved.facts["requires_scorer_ruling"] is True
    assert unresolved.response is None


def test_captured_classification_requires_live_capture_derivation(tmp_path, monkeypatch):
    monkeypatch.setattr(afl_evidence, "FIXTURES_ROOT", tmp_path)
    bogus = {
        "provenance": _base_provenance(
            classification="captured", derivation="afl_api_source_derived", captured_at=None
        ),
        "response": {"seasons": [{"season_id": 1, "is_current": True, "year": 2026}]},
    }
    _write(tmp_path, "v1/captured/season.json", bogus)
    with pytest.raises(EvidenceValidationError, match="requires derivation"):
        afl_evidence.load("v1/captured/season.json")


# --- Malformed/incompatible fixtures are rejected, never silently accepted -


def test_missing_provenance_key_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(afl_evidence, "FIXTURES_ROOT", tmp_path)
    provenance = _base_provenance()
    del provenance["notes"]
    _write(tmp_path, "v1/synthetic/season.json", {"provenance": provenance, "response": {"seasons": []}})
    with pytest.raises(EvidenceValidationError, match="missing required key"):
        afl_evidence.load("v1/synthetic/season.json")


def test_unknown_classification_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(afl_evidence, "FIXTURES_ROOT", tmp_path)
    provenance = _base_provenance(classification="mostly_true")
    _write(tmp_path, "v1/synthetic/season.json", {"provenance": provenance, "response": {"seasons": []}})
    with pytest.raises(EvidenceValidationError, match="unknown classification"):
        afl_evidence.load("v1/synthetic/season.json")


def test_directory_classification_mismatch_is_rejected(tmp_path, monkeypatch):
    """A fixture claiming 'synthetic' but filed under captured/ must fail
    loudly rather than being trusted at face value."""
    monkeypatch.setattr(afl_evidence, "FIXTURES_ROOT", tmp_path)
    provenance = _base_provenance(classification="synthetic")
    _write(tmp_path, "v1/captured/season.json", {"provenance": provenance, "response": {"seasons": []}})
    with pytest.raises(EvidenceValidationError, match="must be under"):
        afl_evidence.load("v1/captured/season.json")


def test_incompatible_response_shape_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(afl_evidence, "FIXTURES_ROOT", tmp_path)
    provenance = _base_provenance(endpoint_kind="matches", endpoint="GET /api/v1/rounds/{round_id}/matches")
    broken_response = {"fixtures": [{"match_id": 1}]}  # wrong wrapper key, missing required fields
    _write(tmp_path, "v1/synthetic/season.json", {"provenance": provenance, "response": broken_response})
    with pytest.raises(EvidenceValidationError, match="expected a 'matches' list"):
        afl_evidence.load("v1/synthetic/season.json")


def test_invalid_json_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(afl_evidence, "FIXTURES_ROOT", tmp_path)
    path = tmp_path / "v1" / "synthetic"
    path.mkdir(parents=True)
    (path / "season.json").write_text("{not valid json")
    with pytest.raises(EvidenceValidationError, match="invalid JSON"):
        afl_evidence.load("v1/synthetic/season.json")


def test_both_response_and_facts_present_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(afl_evidence, "FIXTURES_ROOT", tmp_path)
    provenance = _base_provenance()
    _write(
        tmp_path,
        "v1/synthetic/season.json",
        {"provenance": provenance, "response": {"seasons": []}, "facts": {"requires_scorer_ruling": True}},
    )
    with pytest.raises(EvidenceValidationError, match="exactly one of"):
        afl_evidence.load("v1/synthetic/season.json")


# --- No credentials/secrets in committed fixture metadata -------------------


@pytest.mark.parametrize("bad_key", ["api_key", "X-Api-Key", "token", "Authorization", "password", "secret"])
def test_a_credential_looking_key_anywhere_in_the_fixture_is_rejected(tmp_path, monkeypatch, bad_key):
    monkeypatch.setattr(afl_evidence, "FIXTURES_ROOT", tmp_path)
    provenance = _base_provenance()
    response = {"seasons": [{"season_id": 1, "is_current": True, "year": 2026, bad_key: "should-never-be-here"}]}
    _write(tmp_path, "v1/synthetic/season.json", {"provenance": provenance, "response": response})
    with pytest.raises(EvidenceValidationError, match="credential/secret"):
        afl_evidence.load("v1/synthetic/season.json")


def test_the_committed_corpus_contains_no_credentials_or_secrets():
    for fixture in afl_evidence.iter_all():
        raw = json.dumps({"response": fixture.response, "facts": fixture.facts})
        for forbidden in ("api_key", "x-api-key", "authorization", "password", "secret", "token"):
            assert forbidden not in raw.lower(), f"{fixture.relative_path} looks like it contains a {forbidden!r}"


# --- The whole committed corpus is valid ------------------------------------


def test_the_entire_committed_corpus_loads_and_validates():
    fixtures = afl_evidence.iter_all()
    assert len(fixtures) >= 6
    classifications = {f.provenance.classification for f in fixtures}
    assert classifications == {"synthetic", "unresolved"}  # captured/* are placeholder READMEs, not fixtures


# --- Reuses the real AflApiClient parsing path, with no network possible ---


def test_build_client_wires_the_real_afl_api_client_with_no_socket_possible():
    client = afl_evidence.build_client(
        {"/api/v1/rounds/1500/matches": "v1/synthetic/season_85/round_1500/matches.json"}
    )
    try:
        assert isinstance(client._client._transport, httpx.MockTransport)
        matches = client.get_matches(1500)
        assert {m.match_id for m in matches} == {9501, 9502, 9503}
        assert next(m for m in matches if m.match_id == 9503).state == "completed"
    finally:
        client.close()


def test_build_client_404s_a_path_with_no_curated_route():
    client = afl_evidence.build_client({})
    try:
        with pytest.raises(Exception):
            client.get_matches(1500)
    finally:
        client.close()


# --- helpers -----------------------------------------------------------------


def _base_provenance(**overrides) -> dict:
    provenance = {
        "schema_version": 1,
        "fixture_id": "test-fixture",
        "classification": "synthetic",
        "derivation": "afl_api_source_derived",
        "contract_version": "v1",
        "endpoint": "GET /api/v1/seasons",
        "endpoint_kind": "seasons",
        "request_params": {},
        "season_id": None,
        "round_id": None,
        "match_id": None,
        "canonical_player_ids": [],
        "captured_at": None,
        "authored_at": "2026-08-28T00:00:00Z",
        "source": {"kind": "test"},
        "notes": "A hand-authored fixture used only by this module's own negative tests.",
        "supersedes": None,
        "superseded_by": None,
    }
    provenance.update(overrides)
    return provenance


def _write(root, relative_path, envelope):
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope))
