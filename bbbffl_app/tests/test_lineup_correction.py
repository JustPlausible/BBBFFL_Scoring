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
    service.match_facts = matches  # avoid a real afl-api dependency; see FakeMatchFacts
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


def test_correction_service_error_identifies_the_team_by_name_not_a_bare_uuid():
    """Issue #151: LineupCorrectionService.correct() -- the entry point an
    ordinary Scorer/Admin correction request actually goes through -- must
    identify the affected team by its human-readable name; the
    season_entry_id remains available, but only as a secondary diagnostic."""
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    service = LineupCorrectionService(db, afl_client=None)
    with pytest.raises(NoEffectiveSubmissionError) as excinfo:
        service.correct(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            {},
            expected_submission_version=0,
            actor=SCORER,
            reason="nothing to correct",
        )
    team_name = IdentityRepository(db).get_public_team(entry.season_entry_id).team_name
    message = str(excinfo.value)
    assert message.startswith(team_name)
    assert f"season_entry_id={entry.season_entry_id}" in message


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


# -- Review findings: reserved source, repeated corrections, sign-off race,
# -- stale scorer decisions --------------------------------------------


def test_scorer_correction_source_type_is_rejected_by_ordinary_submission_paths():
    """`source_type='scorer_correction'` must only ever be reachable through
    `submit_correction`, which alone records the correction provenance a
    plain `submit`/`submit_positions` call would silently skip."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    with pytest.raises(LineupIntegrityError, match="reserved"):
        lineups.submit(
            draft.lineup_id,
            expected_draft_revision=draft.revision,
            expected_submission_version=submitted.version,
            source_type="scorer_correction",
        )
    with pytest.raises(LineupIntegrityError, match="reserved"):
        lineups.submit_positions(
            draft.lineup_id,
            dict(submitted.positions),
            expected_submission_version=submitted.version,
            actor=SCORER,
            source_type="scorer_correction",
        )
    assert lineups.get_effective_submission(draft.lineup_id).version == submitted.version
    assert lineups.list_corrections(draft.lineup_id) == []


def test_repeated_correction_of_the_same_locked_position_preserves_lock_provenance():
    """A second correction of a position already corrected once must still
    carry forward the original lock's provenance -- the current occupant at
    correction time is a prior correction's own result, not the row
    `weekly_lineup_lock` was materialized against."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, tackler, bench = (
        _locked_context()
    )
    third = acquire(pool, ownership, scope, entry, 3, LATE_HOME, name="Third Player")
    first_correction = lineups.submit_correction(
        draft.lineup_id,
        {"Tackler": bench.season_player_id, "Interchange": tackler.season_player_id},
        expected_submission_version=submitted.version,
        actor=SCORER,
        reason="first correction of the locked Tackler slot",
    )
    first_tackler_slot = next(s for s in first_correction.slots if s.position == "Tackler")
    assert first_tackler_slot.was_locked is True

    second_correction = lineups.submit_correction(
        draft.lineup_id,
        {"Tackler": third.season_player_id, "Interchange": tackler.season_player_id, "F1": bench.season_player_id},
        expected_submission_version=first_correction.to_version,
        actor=SCORER,
        reason="second correction of the same, already-corrected Tackler slot",
    )
    second_tackler_slot = next(s for s in second_correction.slots if s.position == "Tackler")
    assert second_tackler_slot.was_locked is True
    assert second_tackler_slot.lock_reason == "selective_trigger_activated"
    assert second_tackler_slot.afl_match_id == EARLY_MATCH_ID
    assert second_tackler_slot.previous_season_player_id == bench.season_player_id
    assert second_tackler_slot.corrected_season_player_id == third.season_player_id
    # The immutable lock evidence itself never changes across either correction.
    lock_row = db.execute(
        "SELECT season_player_id FROM weekly_lineup_lock WHERE lineup_id=? AND position='Tackler'",
        (draft.lineup_id,),
    ).fetchone()
    assert lock_row["season_player_id"] == tackler.season_player_id


def test_correction_materializes_lock_evidence_when_none_exists_yet():
    """A correction that is the very first lineup operation since a trigger
    activated must still capture accurate lock provenance. Lock evidence is
    materialized lazily (by `lock_state`/an ordinary submission attempt),
    never eagerly, so `submit_correction` must materialize it itself via
    `lock_guard` rather than assume some prior read already did."""
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    tackler = acquire(pool, ownership, scope, entry, 1, EARLY_HOME, name="James Rowbottom")
    bench = acquire(pool, ownership, scope, entry, 2, LATE_HOME, name="Bench Player")
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    draft, submitted = establish(
        lineups,
        round_,
        entry,
        scope,
        {"Tackler": tackler.season_player_id, "Interchange": bench.season_player_id},
    )
    # Deliberately no prior lock_state()/materialize call: weekly_lineup_lock
    # has no row yet even though the trigger has, by this evaluation
    # instant, already activated.
    assert db.execute("SELECT 1 FROM weekly_lineup_lock WHERE lineup_id=?", (draft.lineup_id,)).fetchone() is None

    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=5))
    correction = lineups.submit_correction(
        draft.lineup_id,
        {"Tackler": bench.season_player_id, "Interchange": tackler.season_player_id},
        expected_submission_version=submitted.version,
        actor=SCORER,
        reason="correcting before anything else ever materialized the lock",
        lock_guard=guard,
    )
    tackler_slot = next(s for s in correction.slots if s.position == "Tackler")
    assert tackler_slot.was_locked is True
    assert tackler_slot.lock_reason == "selective_trigger_activated"
    assert tackler_slot.afl_match_id == EARLY_MATCH_ID
    # Lock evidence is now durably materialized for future reads too.
    lock_row = db.execute(
        "SELECT season_player_id FROM weekly_lineup_lock WHERE lineup_id=? AND position='Tackler'",
        (draft.lineup_id,),
    ).fetchone()
    assert lock_row["season_player_id"] == tackler.season_player_id


def test_correction_bumps_matchup_review_version_closing_the_signoff_race():
    """A correction that lands between a review build and sign-off's own
    transactional CAS check must still be caught: `publish_results` only
    re-validates `expected_review_versions`, so a correction must bump that
    counter exactly like a DNP ruling/override already does, or a stale
    pre-correction score could be published."""
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _round_review_setup(2670)
    entry = entries[0]
    review_before = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    matchup = next(
        m for m in review_before.matchups if entry.season_entry_id in (m.home.season_entry_id, m.away.season_entry_id)
    )
    stale_review_versions = {m.matchup_id: m.review_version for m in review_before.matchups}
    stale_results = {m.matchup_id: (m.home.effective_score, m.away.effective_score) for m in review_before.matchups}

    lineups = WeeklyLineupRepository(db)
    entry_index = next(i for i, e in enumerate(entries) if e.season_entry_id == entry.season_entry_id)
    lineup_id = f"lineup-2670-100-{entry_index}"
    submission = lineups.get_effective_submission(lineup_id)
    f1_player, f2_player = submission.positions["F1"], submission.positions["F2"]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(lifecycle.get_round(round_.bbbffl_round_id).season_id, 20)
    for player_id in submission.positions.values():
        if player_id is not None:
            ownership.acquire(player_id, entry.season_entry_id)

    lineups.submit_correction(
        lineup_id,
        {**submission.positions, "F1": f2_player, "F2": f1_player},
        expected_submission_version=submission.version,
        actor=SCORER,
        reason="swap to prove the review_version CAS closes the sign-off race",
    )

    current_review_version = db.execute(
        "SELECT review_version FROM bbbffl_matchup WHERE matchup_id=?", (matchup.matchup_id,)
    ).fetchone()["review_version"]
    assert current_review_version == stale_review_versions[matchup.matchup_id] + 1

    from app.competition_lifecycle import StaleRoundVersionError

    with pytest.raises(StaleRoundVersionError):
        lifecycle.publish_results(
            round_.bbbffl_round_id,
            stale_results,
            actor=SCORER,
            reason="attempted stale publish racing the correction",
            expected_round_version=review_before.round_version,
            expected_review_versions=stale_review_versions,
        )


def test_correction_invalidates_stale_dnp_ruling_and_override_for_the_changed_position():
    """A DNP ruling/override recorded against the pre-correction occupant of
    a slot must never silently keep applying to whoever the correction
    installs there instead."""
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _round_review_setup(2680)
    entry = entries[0]
    matchup = next(
        m
        for m in lifecycle.list_matchups(round_.bbbffl_round_id)
        if entry.season_entry_id in (m.home_season_entry_id, m.away_season_entry_id)
    )
    review_repo.record_dnp_ruling(
        matchup.matchup_id,
        entry.season_entry_id,
        "F1",
        True,
        expected_review_version=1,
        actor=SCORER,
        reason="pre-correction DNP ruling",
    )
    review_repo.record_override(
        matchup.matchup_id,
        entry.season_entry_id,
        "F2",
        99.0,
        10.0,
        "pre-correction override",
        expected_review_version=2,
        actor=SCORER,
    )
    assert "F1" in {slot for _entry, slot in review_repo.get_slot_rulings(matchup.matchup_id)}

    lineups = WeeklyLineupRepository(db)
    entry_index = next(i for i, e in enumerate(entries) if e.season_entry_id == entry.season_entry_id)
    lineup_id = f"lineup-2680-100-{entry_index}"
    submission = lineups.get_effective_submission(lineup_id)
    f1_player, f3_player = submission.positions["F1"], submission.positions["F3"]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(lifecycle.get_round(round_.bbbffl_round_id).season_id, 20)
    for player_id in submission.positions.values():
        if player_id is not None:
            ownership.acquire(player_id, entry.season_entry_id)

    correction = lineups.submit_correction(
        lineup_id,
        {**submission.positions, "F1": f3_player, "F3": f1_player},
        expected_submission_version=submission.version,
        actor=SCORER,
        reason="correct F1/F3 to prove stale DNP ruling is invalidated",
    )
    assert any(s.position == "F1" for s in correction.slots)

    remaining_rulings = review_repo.get_slot_rulings(matchup.matchup_id)
    assert (entry.season_entry_id, "F1") not in remaining_rulings
    remaining_overrides = review_repo.get_overrides(matchup.matchup_id)
    assert (entry.season_entry_id, "F2") in remaining_overrides  # F2 was never touched by this correction

    # review_version starts at 1, then the DNP ruling and the override each
    # bump it once (-> 3), then the correction's own invalidation pass bumps
    # it a final time (-> 4).
    current_review_version = db.execute(
        "SELECT review_version FROM bbbffl_matchup WHERE matchup_id=?", (matchup.matchup_id,)
    ).fetchone()["review_version"]
    assert current_review_version == 4
