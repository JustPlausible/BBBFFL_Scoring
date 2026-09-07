"""Focused domain/read-model coverage for issue #105's operator preflight
(extended by issue #152 for evidence-backed mapping/lockout recommendations)."""

import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.afl_client import Match, Round, Season, Team
from app.audit import ActorContext, AuditEventRepository
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.identity import IdentityRepository
from app.lockouts import LockoutRepository, LockoutTriggerRepository, RoundMatchFactsProvider
from app.replay import ReplayClock
from app.round_mapping import RoundMappingRepository
from app.round_preflight import (
    StaleMappingRevisionError,
    StaleTriggerRevisionError,
    TriggerValidationError,
    accept_preflight_mapping,
    build_round_preflight,
    configure_preflight_trigger,
)
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


class EvidenceWithSeasons(Evidence):
    """An `Evidence` double that also supports issue #152's human-readable
    season/round listing -- kept as a distinct subclass so the base
    `Evidence` (used throughout this file's pre-#152 coverage) never grows
    `get_seasons` and therefore never surfaces a mapping recommendation or
    an `afl_seasons_unavailable` advisory where a test does not expect one."""

    def __init__(self, matches, seasons, rounds_by_season=None):
        super().__init__(matches)
        self._seasons = seasons
        self._rounds_by_season = rounds_by_season or {}

    def get_seasons(self):
        return self._seasons

    def get_rounds(self, season_id):
        return self._rounds_by_season.get(season_id, [])


class ReplayLikeEvidence(Evidence):
    """An `Evidence` double carrying a `clock` attribute -- the same
    duck-typed replay-metadata signal `app.round_preflight` and
    `app.lockouts.RoundMatchFactsProvider` use to detect replay mode."""

    def __init__(self, matches, clock):
        super().__init__(matches)
        self.clock = clock


class TriggerPayload:
    """A minimal stand-in for `app.routes.round_preflight.TriggerRequest`,
    used to call `configure_preflight_trigger` directly without importing
    a route-layer Pydantic model into these domain-level tests."""

    def __init__(self, trigger_key, trigger_type, sequence, afl_match_ids, expected_revision=None):
        self.trigger_key = trigger_key
        self.trigger_type = trigger_type
        self.sequence = sequence
        self.afl_match_ids = afl_match_ids
        self.expected_revision = expected_revision


def _match(match_id=9001, start_time_utc="2026-03-12T08:30:00+00:00", status="UPCOMING"):
    return Match(match_id, Team(1, "Carlton"), Team(2, "Richmond"), status, start_time_utc)


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
        json={
            "afl_season_id": 2026,
            "afl_round_id": 100,
            "reason": "Secretary confirmed evidence",
            "confirmed": True,
            "expected_revision": 1,
        },
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


# -- Issue #152: evidence-backed mapping/lockout recommendations ------------


def test_mapping_recommendation_reflects_exact_year_and_round_correspondence():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)  # BBBFFL round sequence 1
    evidence = EvidenceWithSeasons(
        [_match()],
        seasons=[Season(season_id=85, is_current=True, current_round_number=1, year=2026, name="2026 Season")],
        rounds_by_season={85: [Round(round_id=1300, round_number=1)]},
    )
    view = build_round_preflight(
        db, CompetitionLifecycleRepository(db), IdentityRepository(db), evidence, round_.bbbffl_round_id
    )
    recommendation = view["mapping_recommendation"]
    assert recommendation == {
        "afl_season_id": 85,
        "afl_round_id": 1300,
        "afl_season_year": 2026,
        "afl_round_number": 1,
        "evidence": recommendation["evidence"],
    }
    assert view["afl_seasons"] == [{"season_id": 85, "year": 2026, "name": "2026 Season", "is_current": True}]


def test_mapping_recommendation_absent_for_ambiguous_multi_season_year_match():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    evidence = EvidenceWithSeasons(
        [_match()],
        seasons=[
            Season(season_id=1, is_current=False, current_round_number=1, year=2026),
            Season(season_id=2, is_current=True, current_round_number=1, year=2026),
        ],
    )
    view = build_round_preflight(
        db, CompetitionLifecycleRepository(db), IdentityRepository(db), evidence, round_.bbbffl_round_id
    )
    assert view["mapping_recommendation"] is None
    assert view["readiness"]["safe_to_open"] is False  # unrelated to the recommendation: no main trigger yet


def test_accept_preflight_mapping_requires_confirmation_and_reason():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    lifecycle = CompetitionLifecycleRepository(db)
    evidence = Evidence([_match()])
    actor = ActorContext.anonymous_operator("admin")
    with pytest.raises(ValueError, match="confirmation"):
        accept_preflight_mapping(
            db, lifecycle, evidence, round_.bbbffl_round_id, 2026, 101, actor=actor, reason="A reason", confirmed=False
        )
    with pytest.raises(ValueError, match="reason"):
        accept_preflight_mapping(
            db, lifecycle, evidence, round_.bbbffl_round_id, 2026, 101, actor=actor, reason="", confirmed=True
        )
    assert RoundMappingRepository(db).resolve(round_.bbbffl_round_id).afl_round_id == 100  # untouched


def test_accept_preflight_mapping_rejects_stale_revision_without_mutating_current_mapping():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)  # accepted at revision 1
    lifecycle = CompetitionLifecycleRepository(db)
    evidence = Evidence([_match()])
    actor = ActorContext.anonymous_operator("admin")
    with pytest.raises(StaleMappingRevisionError):
        accept_preflight_mapping(
            db,
            lifecycle,
            evidence,
            round_.bbbffl_round_id,
            2026,
            100,
            actor=actor,
            reason="retry",
            confirmed=True,
            expected_revision=0,  # stale: the caller never observed revision 1
        )
    current = RoundMappingRepository(db).resolve(round_.bbbffl_round_id)
    assert current.revision == 1 and current.afl_round_id == 100


def test_accept_preflight_mapping_allows_deliberate_divergence_from_recommendation():
    """BBBFFL and AFL round numbering can legitimately diverge -- an
    operator must be able to accept a mapping other than the recommended
    one, provided they explicitly confirm it with a reason."""
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    lifecycle = CompetitionLifecycleRepository(db)
    evidence = EvidenceWithSeasons(
        [_match()],
        seasons=[Season(season_id=85, is_current=True, current_round_number=1, year=2026)],
        rounds_by_season={85: [Round(round_id=1300, round_number=1)], 2026: [Round(round_id=999, round_number=5)]},
    )
    recommendation_view = build_round_preflight(
        db, CompetitionLifecycleRepository(db), IdentityRepository(db), evidence, round_.bbbffl_round_id
    )
    assert recommendation_view["mapping_recommendation"]["afl_season_id"] == 85  # not what we're about to accept
    accepted = accept_preflight_mapping(
        db,
        lifecycle,
        evidence,
        round_.bbbffl_round_id,
        2026,
        999,
        actor=ActorContext.anonymous_operator("admin"),
        reason="This BBBFFL round deliberately maps to a different AFL round this week",
        confirmed=True,
        expected_revision=1,
    )
    assert accepted.afl_season_id == 2026 and accepted.afl_round_id == 999


def test_afl_matches_are_shown_in_scheduled_start_chronological_order():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    middle = _match(9001, "2026-03-14T08:00:00+00:00")
    earliest = _match(9002, "2026-03-12T08:00:00+00:00")
    unscheduled = _match(9003, None)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [9001, 9002, 9003])
    view = _view(db, round_, matches=(middle, earliest, unscheduled))
    assert [m["match_id"] for m in view["afl_matches"]] == [9002, 9001, 9003]  # unscheduled sorts last, never first


def test_lockout_recommendation_absent_when_any_match_lacks_a_scheduled_start():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    matches = (_match(9001, None), _match(9002, "2026-03-12T08:00:00+00:00"))
    view = _view(db, round_, matches=matches)
    assert view["lockout_recommendation"] is None


def test_lockout_recommendation_suggests_selective_then_main_by_earliest_shared_start():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    earliest = _match(9001, "2026-03-12T08:00:00+00:00")
    later_1 = _match(9002, "2026-03-14T08:00:00+00:00")
    later_2 = _match(9003, "2026-03-14T08:00:00+00:00")
    view = _view(db, round_, matches=(earliest, later_1, later_2))
    stages = view["lockout_recommendation"]["stages"]
    assert stages[0]["trigger_type"] == "selective" and stages[0]["afl_match_ids"] == [9001]
    assert stages[1]["trigger_type"] == "main" and sorted(stages[1]["afl_match_ids"]) == [9002, 9003]
    # Purely advisory data -- never itself a persisted trigger.
    assert LockoutTriggerRepository(db).list_triggers(round_.bbbffl_round_id) == []


def test_lockout_recommendation_collapses_to_single_main_when_every_match_shares_one_start_time():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    same_time = "2026-03-12T08:00:00+00:00"
    view = _view(db, round_, matches=(_match(9001, same_time), _match(9002, same_time)))
    stages = view["lockout_recommendation"]["stages"]
    assert len(stages) == 1 and stages[0]["trigger_type"] == "main"
    assert sorted(stages[0]["afl_match_ids"]) == [9001, 9002]


def test_configure_preflight_trigger_rejects_matches_outside_accepted_mapping():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    evidence = Evidence([_match(9001)])
    with pytest.raises(TriggerValidationError, match="not part of the currently accepted mapping"):
        configure_preflight_trigger(
            db,
            round_.bbbffl_round_id,
            TriggerPayload("main", "main", 1, [9999]),
            evidence,
            actor=ActorContext.anonymous_operator("admin"),
            reason="test",
        )
    assert LockoutTriggerRepository(db).list_triggers(round_.bbbffl_round_id) == []


def test_configure_preflight_trigger_requires_an_accepted_mapping():
    db = migrated_connection()
    round_ = SeasonRepository(db).create_round(configured(db, 2026, 100)[0].competition_id, "unmapped", "Unmapped", 2)
    evidence = Evidence([_match(9001)])
    with pytest.raises(TriggerValidationError, match="accepted AFL mapping is required"):
        configure_preflight_trigger(
            db,
            round_.bbbffl_round_id,
            TriggerPayload("main", "main", 1, [9001]),
            evidence,
            actor=ActorContext.anonymous_operator("admin"),
            reason="test",
        )


def test_configure_preflight_trigger_enforces_unique_sequence_numbers():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    evidence = Evidence([_match(9001), _match(9002)])
    actor = ActorContext.anonymous_operator("admin")
    configure_preflight_trigger(
        db, round_.bbbffl_round_id, TriggerPayload("early", "selective", 1, [9001]), evidence, actor=actor, reason="e"
    )
    with pytest.raises(TriggerValidationError, match="already used"):
        configure_preflight_trigger(
            db, round_.bbbffl_round_id, TriggerPayload("dup", "selective", 1, [9002]), evidence, actor=actor, reason="d"
        )


def test_configure_preflight_trigger_enforces_selective_before_main_sequence_ordering():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    evidence = Evidence([_match(9001), _match(9002)])
    actor = ActorContext.anonymous_operator("admin")
    configure_preflight_trigger(
        db, round_.bbbffl_round_id, TriggerPayload("main", "main", 10, [9002]), evidence, actor=actor, reason="m"
    )
    with pytest.raises(TriggerValidationError, match="must precede"):
        configure_preflight_trigger(
            db,
            round_.bbbffl_round_id,
            TriggerPayload("early", "selective", 20, [9001]),
            evidence,
            actor=actor,
            reason="late early",
        )


def test_configure_preflight_trigger_enforces_main_after_every_selective_sequence():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    evidence = Evidence([_match(9001), _match(9002)])
    actor = ActorContext.anonymous_operator("admin")
    configure_preflight_trigger(
        db, round_.bbbffl_round_id, TriggerPayload("early", "selective", 5, [9001]), evidence, actor=actor, reason="e"
    )
    with pytest.raises(TriggerValidationError, match="must follow"):
        configure_preflight_trigger(
            db,
            round_.bbbffl_round_id,
            TriggerPayload("main", "main", 3, [9002]),
            evidence,
            actor=actor,
            reason="early main",
        )


def test_configure_preflight_trigger_rejects_stale_revision():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    evidence = Evidence([_match(9001)])
    actor = ActorContext.anonymous_operator("admin")
    configure_preflight_trigger(
        db, round_.bbbffl_round_id, TriggerPayload("main", "main", 1, [9001]), evidence, actor=actor, reason="m"
    )
    with pytest.raises(StaleTriggerRevisionError):
        configure_preflight_trigger(
            db,
            round_.bbbffl_round_id,
            TriggerPayload("main", "main", 1, [9001], expected_revision=0),
            evidence,
            actor=actor,
            reason="stale retry",
        )


def test_trigger_activation_is_shown_separately_from_observed_afl_status():
    """Issue #152: a match's own *current* AFL status must never be
    conflated with whether BBBFFL's own trigger has actually (durably)
    activated -- here the match still reads UPCOMING from AFL evidence
    while the trigger has activated purely because the evaluated instant
    reached its scheduled start (`evaluate_match_lock`'s time-based
    fallback; see app/lockouts.py)."""
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    match = _match(9001)  # UPCOMING, starts 2026-03-12T08:30:00+00:00
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [9001])
    evidence = Evidence([match])
    match_facts = RoundMatchFactsProvider(RoundMappingRepository(db), evidence)
    LockoutRepository(db).materialize_round_triggers(
        round_.bbbffl_round_id,
        match_facts=match_facts,
        evaluation_at=datetime(2026, 3, 12, 9, 0, tzinfo=timezone.utc),
    )
    view = _view(db, round_, matches=(match,))
    trigger_view = view["lockout_triggers"][0]
    assert trigger_view["activation"]["activated"] is True
    assert trigger_view["activation"]["activation_reason"] == "match_time_reached"
    match_view = view["afl_matches"][0]
    assert match_view["status"] == "UPCOMING"  # the AFL feed's own observed status is untouched
    coverage = match_view["lockout_trigger_coverage"]
    assert coverage == [{"trigger_key": "main", "trigger_type": "main", **trigger_view["activation"]}]


def test_trigger_not_yet_activated_is_reported_as_such_without_any_materialization_call():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [9001])
    view = _view(db, round_)
    assert view["lockout_triggers"][0]["activation"] == {
        "activated": False,
        "activation_reason": None,
        "effective_lock_at": None,
        "observed_status_at_activation": None,
    }


def test_replay_checkpoint_recommendations_are_advisory_and_absent_for_live_clients():
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [9001])
    matches = (_match(9001),)  # still UPCOMING -- not yet concluded

    live_view = _view(db, round_, matches=matches)
    assert live_view["replay_checkpoint_recommendations"] == []

    replay_evidence = ReplayLikeEvidence(list(matches), clock=object())
    replay_view = build_round_preflight(
        db, CompetitionLifecycleRepository(db), IdentityRepository(db), replay_evidence, round_.bbbffl_round_id
    )
    recommendations = replay_view["replay_checkpoint_recommendations"]
    assert recommendations, "replay metadata should produce at least the 'just after trigger' recommendation"
    assert all(r["stage"] == "scheduled" for r in recommendations)
    # No conclusion evidence yet -> no final-results recommendation at all.
    assert not any(r["stage"] == "final-results" for r in recommendations)
    # Never a host filesystem path -- only stage/instant/evidence text.
    assert all("path" not in r and "file" not in r for r in recommendations)


def test_final_results_checkpoint_recommendation_requires_concluded_match_evidence_not_start_time():
    """Codex review (P1) on issue #152's PR: recommending the latest
    match's *scheduled start* as the final-results instant would suggest
    finalising the round the moment its last match begins, not once it has
    actually concluded. The recommendation must instead be "now", and only
    once every relevant match's own currently observed status already
    reads as concluded."""
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [9001])
    concluded_match = _match(9001, status="CONCLUDED")
    now = ReplayClock(datetime(2026, 3, 12, 11, 0, tzinfo=timezone.utc))

    replay_evidence = ReplayLikeEvidence([concluded_match], clock=now)
    replay_view = build_round_preflight(
        db, CompetitionLifecycleRepository(db), IdentityRepository(db), replay_evidence, round_.bbbffl_round_id
    )
    recommendations = replay_view["replay_checkpoint_recommendations"]
    final_results = next(r for r in recommendations if r["stage"] == "final-results")
    assert final_results["recommended_effective_at"] == "2026-03-12T11:00:00+00:00"  # "now", not the match's start


class _AnyAflRoundExists:
    def round_exists(self, season_id, round_id):
        return True


def test_configure_trigger_rejects_a_write_against_a_since_corrected_mapping():
    """Issue #152 review (second pass, P2): `configure_preflight_trigger`
    checks match membership against the mapping it observed, but that
    membership check happens outside any lock. If the accepted mapping is
    corrected between that check and `LockoutTriggerRepository.configure`'s
    write, the trigger must not be silently persisted against the
    now-superseded mapping's matches -- `expected_mapping_revision` closes
    this by re-checking, atomically under `configure`'s own lock, that the
    round's accepted mapping is still the one membership was verified
    against."""
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    observed_mapping = RoundMappingRepository(db).resolve(round_.bbbffl_round_id)
    # A correction lands after membership was checked against `observed_mapping`.
    RoundMappingRepository(db).correct(
        round_.bbbffl_round_id, 2026, 101, _AnyAflRoundExists(), reason="concurrent correction"
    )
    with pytest.raises(StaleTriggerRevisionError, match="accepted AFL mapping has changed"):
        LockoutTriggerRepository(db).configure(
            round_.bbbffl_round_id,
            "main",
            "main",
            1,
            [9001],
            reason="stale membership",
            expected_mapping_revision=observed_mapping.revision,
        )
    assert LockoutTriggerRepository(db).list_triggers(round_.bbbffl_round_id) == []


def test_final_results_checkpoint_recommendation_never_treats_postgame_as_concluded():
    """Codex review (second pass, P1): `app.afl_client`'s own contract
    deliberately keeps POSTGAME distinct from CONCLUDED -- the siren has
    sounded but afl-api has not yet declared statistics final. Treating
    POSTGAME as "good enough" for a final-results checkpoint would
    recommend checkpointing the round as final while stat corrections
    remain possible."""
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [9001])
    postgame_match = _match(9001, status="POSTGAME")
    replay_evidence = ReplayLikeEvidence(
        [postgame_match], clock=ReplayClock(datetime(2026, 3, 12, 11, 0, tzinfo=timezone.utc))
    )
    view = build_round_preflight(
        db, CompetitionLifecycleRepository(db), IdentityRepository(db), replay_evidence, round_.bbbffl_round_id
    )
    assert not any(r["stage"] == "final-results" for r in view["replay_checkpoint_recommendations"])


def test_configure_preflight_trigger_rejects_stale_cached_match_evidence():
    """Codex review (third pass, P2): a resilient client under a live
    outage can serve its last-known-good cached matches instead of raising.
    Validating trigger membership against a stale list could accept a
    match already dropped from the mapped round, only for it to be
    reported unresolved on the next successful refresh -- so membership
    must be checked inside an evidence batch and rejected unless that read
    was fresh, exactly like `build_round_preflight`'s own match read."""
    db = migrated_connection()
    round_, _ = configured(db, 2026, 100)
    with pytest.raises(TriggerValidationError, match="stale cache"):
        configure_preflight_trigger(
            db,
            round_.bbbffl_round_id,
            TriggerPayload("main", "main", 1, [9001]),
            StaleEvidence([_match(9001)]),
            actor=ActorContext.anonymous_operator("admin"),
            reason="test",
        )
    assert LockoutTriggerRepository(db).list_triggers(round_.bbbffl_round_id) == []
