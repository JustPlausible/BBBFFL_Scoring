"""Focused domain/read-model coverage for issue #105's operator preflight."""

import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.afl_client import Match, Team
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.identity import IdentityRepository
from app.lockouts import LockoutTriggerRepository
from app.round_mapping import RoundMappingRepository
from app.round_preflight import build_round_preflight
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import configured


class Evidence:
    def __init__(self, matches):
        self.matches = matches

    def get_matches(self, round_id):
        return self.matches


class StaleEvidence(Evidence):
    @contextmanager
    def evidence_batch(self):
        yield self

    def is_evidence_fresh(self):
        return False


def _match(match_id=9001):
    return Match(match_id, Team(1, "Carlton"), Team(2, "Richmond"), "UPCOMING", "2026-03-12T08:30:00+00:00")


def _view(db, round_, matches=(_match(),)):
    return build_round_preflight(
        db, CompetitionLifecycleRepository(db), IdentityRepository(db), Evidence(list(matches)), round_.bbbffl_round_id
    )


def test_valid_round_represents_five_named_matchups_afl_evidence_and_match_based_lockout():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 2, [9001])
    view = _view(db, round_)
    assert view["readiness"]["safe_to_open"] is True
    assert len(view["fixture_matchups"]) == 5
    assert view["fixture_matchups"][0]["home_team_name"].startswith("Team")
    assert view["afl_matches"][0]["home_team"] == "Carlton"
    assert view["lockout_triggers"][0]["activating_matches"][0]["match_id"] == 9001
    assert view["lockout_triggers"][0]["scope"] == "All remaining selections"
    assert view["readiness"]["advisories"]  # absence of selective stage is advisory, not blocker


def test_missing_or_ambiguous_mapping_and_invalid_lockout_evidence_fail_closed():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    unmapped = SeasonRepository(db).create_round(round_.competition_id, "round-2", "Round 2", 2)
    missing = _view(db, unmapped)
    assert missing["readiness"]["safe_to_open"] is False
    assert "mapping_missing" in {item["code"] for item in missing["readiness"]["blockers"]}
    RoundMappingRepository(db).propose(
        unmapped.bbbffl_round_id, state="ambiguous", afl_season_id=2026, afl_round_id=101
    )
    ambiguous = _view(db, unmapped)
    assert "mapping_unresolved" in {item["code"] for item in ambiguous["readiness"]["blockers"]}


def test_main_trigger_must_resolve_to_mapped_afl_match_and_opening_state_is_not_manufactured():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [9999])
    view = _view(db, round_)
    codes = {item["code"] for item in view["readiness"]["blockers"]}
    assert "lockout_match_unresolved" in codes
    assert view["opening_round"] == {"applies": False, "deferred_selections": []}


def test_stale_cached_afl_evidence_is_visible_but_blocks_opening():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [9001])
    view = build_round_preflight(
        db,
        CompetitionLifecycleRepository(db),
        IdentityRepository(db),
        StaleEvidence([_match()]),
        round_.bbbffl_round_id,
    )
    assert view["afl_matches"]  # stale cache remains useful diagnostically
    assert view["afl_evidence_fresh"] is False
    assert "afl_evidence_stale" in {item["code"] for item in view["readiness"]["blockers"]}
    assert view["readiness"]["safe_to_open"] is False


@pytest.fixture
def preflight_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def test_mapping_endpoint_refuses_to_change_context_frozen_by_lifecycle(preflight_client):
    db = preflight_client.app.state.database
    round_, _ = configured(db, 2026, 100)
    preflight_client.app.state.lifecycle.create_ordinary_round(round_.bbbffl_round_id)
    response = preflight_client.post(
        f"/api/admin/round-preflight/{round_.bbbffl_round_id}/mapping",
        json={"afl_season_id": 2026, "afl_round_id": 101, "reason": "Review regression"},
    )
    assert response.status_code == 409
    assert "lifecycle has already frozen" in response.json()["detail"]
    assert RoundMappingRepository(db).resolve(round_.bbbffl_round_id).afl_round_id == 100


def test_open_endpoint_refuses_stale_cached_afl_evidence(preflight_client):
    db = preflight_client.app.state.database
    round_, _ = configured(db, 2026, 100)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [9001])
    preflight_client.app.state.afl_client = StaleEvidence([_match()])
    response = preflight_client.post(f"/api/admin/round-preflight/{round_.bbbffl_round_id}/open", json={})
    assert response.status_code == 409
    assert preflight_client.app.state.lifecycle.get_round(round_.bbbffl_round_id) is None
    assert response.json()["detail"]["blockers"][0]["code"] == "afl_evidence_stale"


def test_browser_round_index_uses_recognisable_labels_and_preflight_url(preflight_client):
    db = preflight_client.app.state.database
    round_, _ = configured(db, 2026, 100)
    response = preflight_client.get("/api/admin/round-preflight")
    assert response.status_code == 200
    item = response.json()["rounds"][0]
    assert item["round_label"] == "Round 1"
    assert item["season_label"] == "2026"
    assert item["preflight_url"] == f"/admin/round-preflight/{round_.bbbffl_round_id}"
    page = preflight_client.get("/admin/round-preflight")
    assert "Choose the recognisable BBBFFL round" in page.text
