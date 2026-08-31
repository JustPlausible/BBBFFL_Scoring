"""Focused domain/read-model coverage for issue #105's operator preflight."""

import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.afl_client import Match, Team
from app.audit import ActorContext, AuditEventRepository
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

    def get_rounds(self, season_id):
        return [type("Round", (), {"round_id": 100})()]


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


def test_open_endpoint_refuses_stale_cached_afl_evidence(preflight_client, monkeypatch):
    db = preflight_client.app.state.database
    round_, _ = configured(db, 2026, 100)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [9001])
    # The FastAPI app is a module-level singleton shared by the full Python
    # suite.  Patch through pytest so the real client is restored after this
    # assertion; assigning app.state directly leaked the stale double into
    # follow-on replay/checkpoint tests in the same worker.
    real_client = preflight_client.app.state.afl_client
    with monkeypatch.context() as patch:
        patch.setattr(preflight_client.app.state, "afl_client", StaleEvidence([_match()]))
        response = preflight_client.post(f"/api/admin/round-preflight/{round_.bbbffl_round_id}/open", json={})
    assert preflight_client.app.state.afl_client is real_client
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


def test_authenticated_preflight_happy_path_retains_operator_provenance_and_freezes_context(
    preflight_client, monkeypatch
):
    """The complete operator workflow crosses the authenticated HTTP boundary.

    A represented entry deliberately belongs to somebody else: representation
    scopes the Secretary's work and must never replace the human audit actor.
    """
    app = preflight_client.app
    db = app.state.database
    round_, entries = configured(db, 2026, 100)
    season_id = db.execute(
        "SELECT competition_stream.season_id FROM bbbffl_round JOIN competition_stream USING (competition_id) "
        "WHERE bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()["season_id"]

    operator = app.state.identities.create_coach("Authenticated Secretary", email="secretary@example.com")
    app.state.credentials.set_password(
        operator.coach_id, "correct horse battery staple", actor=ActorContext.anonymous_operator("admin")
    )
    app.state.role_grants.grant(
        operator.coach_id,
        "secretary",
        season_id=season_id,
        actor=ActorContext.anonymous_operator("admin"),
    )

    login_page = preflight_client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    login = preflight_client.post(
        "/login",
        data={"email": "secretary@example.com", "password": "correct horse battery staple", "csrf_token": token},
        cookies=login_page.cookies,
        follow_redirects=False,
    )
    session = login.cookies["bbbffl_session"]
    account = preflight_client.get("/account", cookies={"bbbffl_session": session})
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', account.text).group(1)
    cookies = {"bbbffl_session": session, "bbbffl_csrf": account.cookies["bbbffl_csrf"]}
    headers = {"X-CSRF-Token": csrf}
    assert (
        preflight_client.post(
            "/api/context/role", json={"role": "secretary"}, cookies=cookies, headers=headers
        ).status_code
        == 200
    )
    represented = entries[0]
    represented_coach_id = app.state.identities.get_current_coach(represented.season_entry_id).coach_id
    assert (
        preflight_client.post(
            "/api/context/represented-entry",
            json={"season_entry_id": represented.season_entry_id},
            cookies=cookies,
            headers=headers,
        ).status_code
        == 200
    )

    evidence = Evidence([_match(9001), _match(9002)])
    monkeypatch.setattr(app.state, "afl_client", evidence)
    url = f"/api/admin/round-preflight/{round_.bbbffl_round_id}"
    initial = preflight_client.get(url, cookies=cookies)
    assert initial.status_code == 200
    assert initial.json()["readiness"]["safe_to_open"] is False
    assert {b["code"] for b in initial.json()["readiness"]["blockers"]} == {"main_lockout_incomplete"}

    mapped = preflight_client.post(
        f"{url}/mapping",
        json={"afl_season_id": 2026, "afl_round_id": 100, "reason": "Secretary confirmed evidence"},
        cookies=cookies,
        headers=headers,
    )
    assert mapped.status_code == 200, mapped.text
    assert mapped.json()["mapping"]["afl_round_id"] == 100

    for payload in (
        {"trigger_key": "early", "trigger_type": "selective", "sequence": 1, "afl_match_ids": [9001]},
        {"trigger_key": "main", "trigger_type": "main", "sequence": 2, "afl_match_ids": [9002]},
    ):
        response = preflight_client.post(f"{url}/lockout-trigger", json=payload, cookies=cookies, headers=headers)
        assert response.status_code == 200, response.text

    ready = preflight_client.get(url, cookies=cookies).json()
    assert ready["readiness"] == {"safe_to_open": True, "blockers": [], "advisories": []}
    opened = preflight_client.post(f"{url}/open", json={}, cookies=cookies, headers=headers)
    assert opened.status_code == 200, opened.text
    assert opened.json()["round"]["lifecycle_state"] == "open"
    frozen = app.state.lifecycle.get_round(round_.bbbffl_round_id)
    assert frozen.afl_season_id == 2026 and frozen.afl_round_id == 100
    assert len(app.state.lifecycle.list_matchups(round_.bbbffl_round_id)) == 5

    events = AuditEventRepository(db).list_events()
    workflow = [
        event
        for event in events
        if event.action
        in {
            "round_mapping.corrected",
            "lockout.trigger.configured",
            "competition.round.created",
            "competition.round.transitioned",
        }
    ]
    assert [event.action for event in workflow] == [
        "round_mapping.corrected",
        "lockout.trigger.configured",
        "lockout.trigger.configured",
        "competition.round.created",
        "competition.round.transitioned",
    ]
    assert all(event.actor_type == "anonymous_operator" for event in workflow)
    assert all(event.actor_id == operator.coach_id and event.actor_role == "secretary" for event in workflow)
    assert all(event.actor_id != represented_coach_id for event in workflow)

    rejected = preflight_client.post(
        f"{url}/mapping",
        json={"afl_season_id": 2026, "afl_round_id": 100, "reason": "Must remain frozen"},
        cookies=cookies,
        headers=headers,
    )
    assert rejected.status_code == 409
    assert RoundMappingRepository(db).resolve(round_.bbbffl_round_id).revision == 2
