"""Domain tests for issue #137: audited Scorer/Admin correction of an
already-locked weekly lineup.

`_locked_context()` reproduces the concrete 2026 replay scenario the issue
describes: James Rowbottom entered at Tackler before a selective lockout
activated for his AFL match, with the historical record showing Interchange
instead. Everything here proves the correction workflow can fix exactly
that, atomically and with full audited provenance, without weakening any
existing lockout/ownership/Opening-Round invariant.
"""

from datetime import timedelta

import pytest

from app.audit import ENTITY_TYPE_LINEUP, LINEUP_CORRECTED, ActorContext, AuditEventRepository
from app.calculations import MatchupCalculationService
from app.identity import IdentityRepository
from app.lineup_correction import LineupCorrectionService, UnauthorizedCorrectionActorError
from app.lineup_proxy import LineupProxyService
from app.lineups import (
    LineupConflictError,
    LineupIntegrityError,
    NoEffectiveSubmissionError,
    NoOpCorrectionError,
    RoundPublishedError,
    WeeklyLineupRepository,
)
from app.lockouts import LockedSelectionError, LockoutRepository, LockoutTriggerRepository
from app.opening_round import OpeningRoundNominationRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.round_review import RoundReviewRepository, attempt_signoff, build_round_review
from tests.db_helpers import migrated_connection
from tests.round_review_helpers import Facts, full_round, progress_to_review
from tests.test_lockouts import (
    ALL_MATCHES,
    EARLY_HOME,
    EARLY_MATCH_ID,
    EARLY_START,
    LATE_HOME,
    FakeMatchFacts,
    acquire,
    configure_selective,
    context,
    edit_draft,
    establish,
)
from tests.test_opening_round import nominate_bl_2024, own_player, setup_scope

SCORER = ActorContext.anonymous_operator(role="scorer")
ADMIN = ActorContext.anonymous_operator(role="admin")
REPLAY_OPERATOR = ActorContext.anonymous_operator(role="replay_operator")
COACH = ActorContext.coach("coach-1")


def _locked_context():
    """Tackler locked by an already-active selective trigger; Interchange
    vacant and still editable -- the pre-correction state of the issue's
    concrete scenario."""
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    tackler = acquire(pool, ownership, scope, entry, 1, EARLY_HOME, name="James Rowbottom")
    bench = acquire(pool, ownership, scope, entry, 2, LATE_HOME, name="Bench Player")
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    # Unguarded initial submission -- nothing is locked yet to protect (the
    # trigger is configured but not yet observed as activated); the lock is
    # then materialized explicitly below, exactly as
    # tests/test_lockouts.py's own `_locked_context` does.
    draft, submitted = establish(
        lineups,
        round_,
        entry,
        scope,
        {"Tackler": tackler.season_player_id, "Interchange": bench.season_player_id},
    )
    LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    return db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench


# -- 1/2: existing boundaries stay intact --------------------------------


def test_coach_cannot_move_a_locked_player():
    """Acceptance #1."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"Tackler": None, "Interchange": tackler.season_player_id},
        from_revision=draft.revision,
    )
    with pytest.raises(LockedSelectionError):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard,
        )
    assert lineups.get_effective_submission(draft.lineup_id).version == submitted.version


def test_ordinary_proxy_entry_cannot_move_a_locked_player():
    """Acceptance #2: generic `lineup.proxy` authority is not sufficient."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    proxy = LineupProxyService(db)
    proxy_draft = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"Tackler": None, "Interchange": tackler.season_player_id},
        expected_revision=draft.revision,
        actor=SCORER,
    )
    with pytest.raises(LockedSelectionError):
        proxy.submit(
            proxy_draft.lineup_id,
            expected_draft_revision=proxy_draft.revision,
            expected_submission_version=submitted.version,
            actor=SCORER,
            reason="ordinary proxy attempt",
            lock_guard=guard,
        )
    assert lineups.get_effective_submission(draft.lineup_id).version == submitted.version


# -- 3-7: the authorised correction itself -------------------------------


def test_scorer_can_atomically_correct_a_locked_player_with_full_audited_provenance():
    """Acceptance #3-7."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    original_lock = dict(
        db.execute(
            "SELECT * FROM weekly_lineup_lock WHERE lineup_id=? AND position='Tackler'", (draft.lineup_id,)
        ).fetchone()
    )
    assert original_lock["season_player_id"] == tackler.season_player_id
    assert original_lock["lock_reason"] == "selective_trigger_activated"
    assert original_lock["afl_match_id"] == EARLY_MATCH_ID

    reason = (
        "League chat confirmed James Rowbottom was named Interchange, not Tackler; "
        "scorer transcription error at delegated entry time"
    )
    correction = lineups.submit_correction(
        draft.lineup_id,
        {"Tackler": bench.season_player_id, "Interchange": tackler.season_player_id},
        expected_submission_version=submitted.version,
        actor=SCORER,
        reason=reason,
    )

    # 4. Previous submission remains unchanged/readable.
    previous = lineups.get_submission(draft.lineup_id, submitted.version)
    assert previous == submitted
    assert previous.positions["Tackler"] == tackler.season_player_id
    assert previous.positions["Interchange"] == bench.season_player_id

    # 5. Original lock evidence remains unchanged/readable.
    still_locked = dict(
        db.execute(
            "SELECT * FROM weekly_lineup_lock WHERE lineup_id=? AND position='Tackler'", (draft.lineup_id,)
        ).fetchone()
    )
    assert still_locked == original_lock

    # 6. Corrected authoritative version becomes effective.
    effective = lineups.get_effective_submission(draft.lineup_id)
    assert effective.version == correction.to_version == submitted.version + 1
    assert correction.from_version == submitted.version
    assert effective.positions["Tackler"] == bench.season_player_id
    assert effective.positions["Interchange"] == tackler.season_player_id
    assert effective.source_type == "scorer_correction"
    assert effective.actor_role == "scorer"
    assert effective.reason == reason

    # 7. Audit provenance: before/after, actor, role, reason, active trigger.
    events = AuditEventRepository(db).list_events(
        entity_type=ENTITY_TYPE_LINEUP, entity_id=draft.lineup_id, action=LINEUP_CORRECTED
    )
    assert len(events) == 1
    event = events[0]
    assert event.actor_role == "scorer"
    assert event.reason == reason
    assert event.before_state["positions"]["Tackler"] == tackler.season_player_id
    assert event.after_state["positions"]["Tackler"] == bench.season_player_id
    assert event.payload["correction_id"] == correction.correction_id
    assert "Tackler" in event.payload["locked_positions_overridden"]

    tackler_slot = next(s for s in correction.slots if s.position == "Tackler")
    assert tackler_slot.was_locked is True
    assert tackler_slot.lock_reason == "selective_trigger_activated"
    assert tackler_slot.afl_match_id == EARLY_MATCH_ID
    assert tackler_slot.previous_season_player_id == tackler.season_player_id
    assert tackler_slot.corrected_season_player_id == bench.season_player_id
    interchange_slot = next(s for s in correction.slots if s.position == "Interchange")
    assert interchange_slot.was_locked is False

    history = lineups.list_corrections(draft.lineup_id)
    assert [c.correction_id for c in history] == [correction.correction_id]
    assert lineups.get_correction(correction.correction_id) == correction


def test_correction_service_merges_partial_change_and_enforces_actor(monkeypatch):
    """`LineupCorrectionService.correct` accepts only the changed slots and
    merges them onto the current effective lineup, still validating the
    complete final state."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    service = LineupCorrectionService(db, afl_client=None)
    correction = service.correct(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"Tackler": bench.season_player_id, "Interchange": tackler.season_player_id},
        expected_submission_version=submitted.version,
        actor=ADMIN,
        reason="Admin-authorised swap via the partial-change service entry point",
    )
    effective = lineups.get_effective_submission(draft.lineup_id)
    assert effective.positions["Tackler"] == bench.season_player_id
    assert effective.positions["F1"] is None  # untouched positions survive the merge unchanged
    assert correction.actor_role == "admin"


# -- 8: unauthorized actor ------------------------------------------------


def test_unauthorized_actor_correction_fails():
    """Acceptance #8: neither an authenticated coach nor an unrecognised
    operator role may correct a locked lineup."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    service = LineupCorrectionService(db, afl_client=None)
    with pytest.raises(UnauthorizedCorrectionActorError):
        service.correct(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            {"Tackler": bench.season_player_id, "Interchange": tackler.season_player_id},
            expected_submission_version=submitted.version,
            actor=COACH,
            reason="a coach should never be able to do this",
        )
    unrecognised = ActorContext.anonymous_operator(role="secretary")
    with pytest.raises(UnauthorizedCorrectionActorError):
        service.correct(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            {"Tackler": bench.season_player_id, "Interchange": tackler.season_player_id},
            expected_submission_version=submitted.version,
            actor=unrecognised,
            reason="secretary is not an authorised correction role",
        )
    assert lineups.get_effective_submission(draft.lineup_id).version == submitted.version


def test_replay_operator_may_correct():
    """Replay Operator is a permitted correction role (season-scoping itself
    is enforced at the HTTP layer via `require_role_covers_season` -- see
    tests/test_lineup_correction_api.py)."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    correction = lineups.submit_correction(
        draft.lineup_id,
        {"Tackler": bench.season_player_id, "Interchange": tackler.season_player_id},
        expected_submission_version=submitted.version,
        actor=REPLAY_OPERATOR,
        reason="Replay operator authorised for this replay season",
    )
    assert correction.actor_role == "replay_operator"


# -- 10: stale expected version -------------------------------------------


def test_stale_expected_version_fails():
    """Acceptance #10."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    with pytest.raises(LineupConflictError):
        lineups.submit_correction(
            draft.lineup_id,
            {"Tackler": bench.season_player_id, "Interchange": tackler.season_player_id},
            expected_submission_version=submitted.version + 1,
            actor=SCORER,
            reason="based on a stale version",
        )
    assert lineups.get_effective_submission(draft.lineup_id).version == submitted.version
    assert lineups.list_corrections(draft.lineup_id) == []


# -- 12-14: atomic server-side validation ----------------------------------


def test_duplicate_player_final_state_fails_atomically():
    """Acceptance #12."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    with pytest.raises(LineupIntegrityError, match="multiple"):
        lineups.submit_correction(
            draft.lineup_id,
            {"Tackler": bench.season_player_id, "Interchange": bench.season_player_id},
            expected_submission_version=submitted.version,
            actor=SCORER,
            reason="duplicate player attempt",
        )
    assert lineups.get_effective_submission(draft.lineup_id).version == submitted.version
    assert lineups.list_corrections(draft.lineup_id) == []


def test_invalid_position_fails_atomically():
    """Acceptance #13."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    with pytest.raises(LineupIntegrityError, match="unknown"):
        lineups.submit_correction(
            draft.lineup_id,
            {"NotAScoringPosition": bench.season_player_id},
            expected_submission_version=submitted.version,
            actor=SCORER,
            reason="invalid position attempt",
        )
    assert lineups.get_effective_submission(draft.lineup_id).version == submitted.version
    assert lineups.list_corrections(draft.lineup_id) == []


def test_unowned_player_fails_atomically():
    """Acceptance #14: this check happens mid-transaction (inside
    `_finalize_submission`), so it is the meaningful atomicity proof --
    nothing partially commits even though the check is not the first one
    performed."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    foreign = PlayerPoolRepository(db).refresh_player(scope["season_id"], 99999, "Unowned Player")
    with pytest.raises(LineupIntegrityError, match="not currently owned"):
        lineups.submit_correction(
            draft.lineup_id,
            {"Interchange": foreign.season_player_id},
            expected_submission_version=submitted.version,
            actor=SCORER,
            reason="unowned player attempt",
        )
    assert lineups.get_effective_submission(draft.lineup_id).version == submitted.version
    assert lineups.list_corrections(draft.lineup_id) == []
    assert (
        db.execute(
            "SELECT 1 FROM weekly_lineup_submission_slot WHERE season_player_id=?", (foreign.season_player_id,)
        ).fetchone()
        is None
    )


def test_no_effective_submission_cannot_be_corrected():
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    lineups = WeeklyLineupRepository(db)
    lineup_id, _ = lineups.get_or_create_header(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    with pytest.raises(NoEffectiveSubmissionError):
        lineups.submit_correction(
            lineup_id, {}, expected_submission_version=0, actor=SCORER, reason="nothing to correct"
        )


def test_correction_with_no_actual_change_is_rejected():
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    with pytest.raises(NoOpCorrectionError):
        lineups.submit_correction(
            draft.lineup_id,
            dict(submitted.positions),
            expected_submission_version=submitted.version,
            actor=SCORER,
            reason="no real change",
        )


def test_missing_reason_is_rejected():
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    with pytest.raises(LineupIntegrityError, match="reason"):
        lineups.submit_correction(
            draft.lineup_id,
            {"Tackler": bench.season_player_id, "Interchange": tackler.season_player_id},
            expected_submission_version=submitted.version,
            actor=SCORER,
            reason="   ",
        )


# -- 15: Opening Round deferred slots stay out of reach --------------------


def test_opening_round_deferred_slot_cannot_be_corrected_through_this_workflow():
    """Acceptance #15."""
    db = migrated_connection()
    lifecycle, round_, entries, scope = setup_scope(db, 2601, 2601)
    entry = entries[0]
    _rule, nominated_player, _nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, entry, position="F1"
    )
    lineups = WeeklyLineupRepository(db)
    OpeningRoundNominationRepository(db).preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    other = own_player(db, scope["season_id"], entry, 700002, "Other Player", afl_team_id=99)
    draft = lineups.get_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    positions = dict(draft.positions)
    positions["M1"] = other.season_player_id
    draft2 = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        positions,
        expected_revision=draft.revision,
    )
    submitted = lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=0)
    assert submitted.positions["F1"] == nominated_player.season_player_id
    replacement = own_player(db, scope["season_id"], entry, 700003, "Replacement Player", afl_team_id=99)
    with pytest.raises(LineupIntegrityError, match="Opening Round deferred"):
        lineups.submit_correction(
            draft2.lineup_id,
            {"F1": replacement.season_player_id},
            expected_submission_version=submitted.version,
            actor=SCORER,
            reason="attempt to bypass the Opening Round deferred slot",
        )
    assert lineups.get_effective_submission(draft2.lineup_id).version == submitted.version


# -- 16: calculation/review invalidation -----------------------------------


def _round_review_setup(year):
    db, lifecycle, round_, entries, stats, canon = full_round(year=year)
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    MatchupCalculationService(db, Facts(stats)).calculate_round(round_.bbbffl_round_id)
    review_repo = RoundReviewRepository(db)
    identities = IdentityRepository(db)
    return db, lifecycle, round_, entries, stats, canon, review_repo, identities


def test_correction_after_calculation_marks_review_stale_and_recalculation_clears_it():
    """Acceptance #16."""
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _round_review_setup(2650)
    entry = entries[0]
    review_before = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    matchup = next(
        m for m in review_before.matchups if entry.season_entry_id in (m.home.season_entry_id, m.away.season_entry_id)
    )
    assert matchup.eligible_for_signoff

    lineups = WeeklyLineupRepository(db)
    entry_index = next(i for i, e in enumerate(entries) if e.season_entry_id == entry.season_entry_id)
    lineup_id = f"lineup-2650-100-{entry_index}"
    submission = lineups.get_effective_submission(lineup_id)
    f1_player, f2_player = submission.positions["F1"], submission.positions["F2"]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(lifecycle.get_round(round_.bbbffl_round_id).season_id, 20)
    # `full_round` names every slot with a distinct raw-inserted player but
    # never establishes ownership (it exists purely for calculation
    # fixtures) -- `submit_correction` re-validates ownership for the whole
    # proposed lineup, not only the changed slots, so every currently-named
    # player must be owned by this entry first.
    for player_id in submission.positions.values():
        if player_id is not None:
            ownership.acquire(player_id, entry.season_entry_id)

    lineups.submit_correction(
        lineup_id,
        {**submission.positions, "F1": f2_player, "F2": f1_player},
        expected_submission_version=submission.version,
        actor=SCORER,
        reason="swap F1/F2 to prove review staleness detection",
    )

    review_after = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    matchup_after = next(m for m in review_after.matchups if m.matchup_id == matchup.matchup_id)
    assert not matchup_after.eligible_for_signoff
    assert any("recalculate" in blocker for blocker in matchup_after.blockers)

    MatchupCalculationService(db, Facts(stats)).calculate_round(round_.bbbffl_round_id)
    review_final = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    matchup_final = next(m for m in review_final.matchups if m.matchup_id == matchup.matchup_id)
    assert matchup_final.eligible_for_signoff


# -- 17: published round refuses correction --------------------------------


def test_published_round_refuses_correction():
    """Acceptance #17."""
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _round_review_setup(2660)
    entry = entries[0]
    attempt_signoff(lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=SCORER, reason="signoff")
    lineups = WeeklyLineupRepository(db)
    entry_index = next(i for i, e in enumerate(entries) if e.season_entry_id == entry.season_entry_id)
    lineup_id = f"lineup-2660-100-{entry_index}"
    submission = lineups.get_effective_submission(lineup_id)
    f1_player, f2_player = submission.positions["F1"], submission.positions["F2"]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(lifecycle.get_round(round_.bbbffl_round_id).season_id, 20)
    ownership.acquire(f1_player, entry.season_entry_id)
    ownership.acquire(f2_player, entry.season_entry_id)
    with pytest.raises(RoundPublishedError):
        lineups.submit_correction(
            lineup_id,
            {**submission.positions, "F1": f2_player, "F2": f1_player},
            expected_submission_version=submission.version,
            actor=SCORER,
            reason="attempt to correct a published round",
        )
    assert lineups.get_effective_submission(lineup_id).version == submission.version
