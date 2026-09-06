"""Domain tests for issue #146: audited Scorer/Admin adjudication of a
missed *initial* weekly-lineup submission after lockout.

Mirrors `tests/test_lineup_correction.py`'s structure and reuses
`tests/test_lockouts.py`'s fixed 2027 match/trigger fixtures and
`tests/test_carry_forward.py`'s multi-round context. Draft-slot
`updated_at` provenance is set directly via raw SQL in these tests (rather
than relying on wall-clock timing) so "saved before/after the lock instant"
is deterministic and independent of when the test suite actually runs --
see `_set_draft_slot_saved_at`.
"""

from datetime import timedelta

import pytest

from app.audit import LINEUP_ADJUDICATED, LINEUP_SUBMITTED, ActorContext, AuditEventRepository
from app.carry_forward import NoCarryForwardSourceError
from app.db import transaction
from app.lineup_adjudication import (
    DECISION_ACCEPT_EVIDENCED_DRAFT,
    DECISION_APPLY_CARRY_FORWARD,
    LineupAdjudicationError,
    LineupAdjudicationService,
    RoundNotEligibleForAdjudicationError,
    UnauthorizedAdjudicationActorError,
)
from app.lineups import (
    ADJUDICATION_ALLOWED_STATES,
    EffectiveSubmissionExistsError,
    LineupIntegrityError,
    WeeklyLineupRepository,
)
from app.lockouts import LockoutRepository, LockoutTriggerRepository
from app.opening_round import OpeningRoundNominationRepository
from tests.db_helpers import migrated_connection
from tests.test_carry_forward import context as carry_forward_context
from tests.test_carry_forward import submit_round
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
)
from tests.test_opening_round import nominate_bl_2024, own_player, setup_scope

SCORER = ActorContext.anonymous_operator(role="scorer")
ADMIN = ActorContext.anonymous_operator(role="admin")
REPLAY_OPERATOR = ActorContext.anonymous_operator(role="replay_operator")
COACH = ActorContext.coach("coach-1")


def _set_draft_slot_saved_at(db, lineup_id, position, saved_at_iso):
    with transaction(db) as conn:
        conn.execute(
            "UPDATE weekly_lineup_draft_slot SET updated_at=? WHERE lineup_id=? AND position=?",
            (saved_at_iso, lineup_id, position),
        )


def _missed_submission_scenario(year=2810):
    """Round `live`, one selective trigger configured on `EARLY_MATCH_ID`,
    a private draft saved (never submitted) naming an early-match player
    and a late-match player, no effective submission for this entry."""
    db, lifecycle, round_, entries, scope, pool, ownership = context(year=year)
    lifecycle.transition(round_.bbbffl_round_id, "live")
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME, name="Early Match Player")
    late = acquire(pool, ownership, scope, entry, 2, LATE_HOME, name="Late Match Player")
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": early.season_player_id, "M1": late.season_player_id},
        expected_revision=0,
        actor=COACH,
    )
    # Pre-lockout saves: F1/M1 both saved well before EARLY_START.
    _set_draft_slot_saved_at(db, draft.lineup_id, "F1", (EARLY_START - timedelta(days=1)).isoformat())
    _set_draft_slot_saved_at(db, draft.lineup_id, "M1", (EARLY_START - timedelta(days=1)).isoformat())
    service = LineupAdjudicationService(db, afl_client=None)
    service.match_facts = matches
    return db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service


def _activate_early_trigger(service, round_id):
    """Durably materialize the early selective trigger's activation, well
    after EARLY_START."""
    service.lockouts.materialize_round_triggers(
        round_id, match_facts=service.match_facts, evaluation_at=EARLY_START + timedelta(hours=1)
    )


# -- Eligibility gate ------------------------------------------------------


def test_refuses_when_effective_submission_already_exists():
    """The service's own eligibility pre-check catches this in the
    ordinary sequential case; `test_domain_primitive_refuses_a_second_
    first_submission_even_past_the_service_pre_check` below proves the
    same fact is *also* enforced, atomically and unconditionally, by the
    domain primitive itself (issue #146's "concurrency-safe rather than
    merely a UI check")."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=2))
    lineups.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=0,
        lock_guard=pre_lock_guard,
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)
    with pytest.raises(RoundNotEligibleForAdjudicationError, match="already exists"):
        service.accept_evidenced_draft(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            actor=SCORER,
            reason="should be refused: a submission already exists",
            evaluation_at=EARLY_START + timedelta(hours=2),
        )


def test_domain_primitive_refuses_a_second_first_submission_even_past_the_service_pre_check():
    """`WeeklyLineupRepository.submit_adjudicated_first_submission` itself
    -- not only the service's non-transactional pre-check -- refuses once
    an effective submission exists, checked under the same row lock its
    own CAS relies on. This is what actually closes the race a
    time-of-check/time-of-use gap between the service's pre-check and its
    transaction would otherwise leave open."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=2))
    lineups.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=0,
        lock_guard=pre_lock_guard,
    )

    def resolve_positions(conn, lineup_row):
        raise AssertionError("must never be called once the effective-submission check rejects the attempt")

    with pytest.raises(EffectiveSubmissionExistsError):
        lineups.submit_adjudicated_first_submission(
            draft.lineup_id,
            actor=SCORER,
            reason="attempt past an existing effective submission",
            source_type="scorer_late_capture",
            source_detail=None,
            resolve_positions=resolve_positions,
            record_adjudication=lambda conn, version: None,
        )


def test_coach_cannot_adjudicate():
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)
    with pytest.raises(UnauthorizedAdjudicationActorError):
        service.accept_evidenced_draft(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            actor=COACH,
            reason="a coach should never be able to do this",
            evaluation_at=EARLY_START + timedelta(hours=2),
        )
    with pytest.raises(UnauthorizedAdjudicationActorError):
        service.apply_carry_forward_fallback(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            actor=COACH,
            reason="a coach should never be able to do this",
            evaluation_at=EARLY_START + timedelta(hours=2),
        )


def test_unrecognised_operator_role_cannot_adjudicate():
    """Ordinary delegated/proxy authority (e.g. a `secretary` role, which
    carries no `lineup.proxy`/`lineup.correct_locked`-style authority
    either) is never sufficient -- only scorer/admin/replay_operator."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)
    unrecognised = ActorContext.anonymous_operator(role="secretary")
    with pytest.raises(UnauthorizedAdjudicationActorError):
        service.accept_evidenced_draft(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            actor=unrecognised,
            reason="secretary is not an authorised adjudication role",
            evaluation_at=EARLY_START + timedelta(hours=2),
        )


def test_final_and_open_rounds_are_excluded_from_the_allowed_states():
    """Issue #146's adjudication gate is deliberately narrower than issue
    #137's correction gate: only `live`/`review` (never `open` -- no lock
    could possibly have activated yet -- and never `final`, exactly like
    every other submission path)."""
    assert ADJUDICATION_ALLOWED_STATES == {"live", "review"}


def test_open_round_refuses_adjudication():
    """A round that has not yet gone live can never have a "missed initial
    submission after lockout" -- no lock could possibly have activated."""
    db, lifecycle, round_, entries, scope, pool, ownership = context(year=2811)
    entry = entries[0]
    lineups = WeeklyLineupRepository(db)
    lineups.get_or_create_header(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    service = LineupAdjudicationService(db, afl_client=None)
    service.match_facts = FakeMatchFacts(ALL_MATCHES)
    with pytest.raises(RoundNotEligibleForAdjudicationError, match="open"):
        service.accept_evidenced_draft(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            actor=SCORER,
            reason="round has not gone live yet",
        )


def test_no_activated_trigger_refuses_adjudication():
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    # Deliberately never activate the configured trigger.
    with pytest.raises(RoundNotEligibleForAdjudicationError, match="no lockout trigger"):
        service.accept_evidenced_draft(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            actor=SCORER,
            reason="nothing has activated yet",
            evaluation_at=EARLY_START - timedelta(days=2),
        )


def test_missing_reason_fails_atomically_with_no_writes():
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)
    with pytest.raises(LineupAdjudicationError, match="substantive"):
        service.accept_evidenced_draft(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            actor=SCORER,
            reason="   ",
            evaluation_at=EARLY_START + timedelta(hours=2),
        )
    assert lineups.get_effective_submission(draft.lineup_id) is None
    assert db.execute("SELECT 1 FROM lineup_adjudication WHERE lineup_id=?", (draft.lineup_id,)).fetchone() is None


# -- Resolution A: accept evidenced pre-lockout draft -----------------------


def test_valid_pre_lockout_draft_becomes_submission_version_one():
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)
    submission, adjudication = service.accept_evidenced_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        actor=SCORER,
        reason="League chat: coach's pre-lockout draft accepted by quorum",
        evaluation_at=EARLY_START + timedelta(hours=2),
    )
    assert submission.version == 1
    assert submission.source_type == "scorer_late_capture"
    assert submission.positions["F1"] == early.season_player_id
    assert submission.positions["M1"] == late.season_player_id
    assert adjudication.decision_type == DECISION_ACCEPT_EVIDENCED_DRAFT
    assert adjudication.submission_version == 1
    assert adjudication.actor_role == "scorer"
    f1_slot = next(s for s in adjudication.slots if s.position == "F1")
    assert f1_slot.was_locked is True
    assert f1_slot.evidence_status == "proven_pre_lock"
    assert f1_slot.season_player_id == early.season_player_id
    m1_slot = next(s for s in adjudication.slots if s.position == "M1")
    assert m1_slot.was_locked is False
    assert m1_slot.evidence_status == "editable_current_draft"


def test_selective_lock_later_unlocked_edit_does_not_contaminate_locked_evidence():
    """The selective-lock scenario the issue's evidence inspection calls
    out: a legitimate post-lockout edit to a still-unlocked position must
    not obscure -- or be mistaken for -- when the already-locked position's
    own value was actually set."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)
    # Coach returns after the lockout and edits the still-unlocked M1 slot
    # to a different player -- F1 is left untouched.
    replacement_late = acquire(pool, ownership, scope, entry, 3, LATE_HOME, name="Replacement Late Player")
    draft2 = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": early.season_player_id, "M1": replacement_late.season_player_id},
        expected_revision=draft.revision,
        actor=COACH,
    )
    assert draft2.revision == draft.revision + 1
    submission, adjudication = service.accept_evidenced_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        actor=SCORER,
        reason="F1 was saved before lockout; M1 was legitimately updated afterward",
        evaluation_at=EARLY_START + timedelta(hours=3),
    )
    # F1's evidence still traces back to its original pre-lock save.
    f1_slot = next(s for s in adjudication.slots if s.position == "F1")
    assert f1_slot.evidence_status == "proven_pre_lock"
    assert submission.positions["F1"] == early.season_player_id
    # M1, never locked, simply reflects the coach's latest (post-lockout,
    # but still-editable) choice.
    assert submission.positions["M1"] == replacement_late.season_player_id


def test_post_lockout_edit_of_a_locked_position_is_not_accepted_as_evidence():
    """A draft edit to an already-locked position, saved after that
    position's own effective lock instant, must never be misrepresented as
    pre-lockout evidence -- it defaults to vacant instead."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)
    # Simulate a post-lock save_draft edit to F1 (save_draft itself has no
    # lock enforcement -- see app/lineups.py's module docstring) by
    # advancing F1's own provenance timestamp to after the lock instant.
    _set_draft_slot_saved_at(db, draft.lineup_id, "F1", (EARLY_START + timedelta(minutes=30)).isoformat())
    submission, adjudication = service.accept_evidenced_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        actor=SCORER,
        reason="F1's draft value cannot be proven pre-lock; must default to vacant",
        evaluation_at=EARLY_START + timedelta(hours=2),
    )
    f1_slot = next(s for s in adjudication.slots if s.position == "F1")
    assert f1_slot.evidence_status == "unproven_defaulted_vacant"
    assert f1_slot.season_player_id is None
    assert submission.positions["F1"] is None
    # The operator had no way to substitute another player for F1 either --
    # accept_evidenced_draft() accepts no position overrides at all.
    assert submission.positions["M1"] == late.season_player_id


def test_unlocked_positions_completable_via_ordinary_submission_after_adjudication():
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)
    submission, _adjudication = service.accept_evidenced_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        actor=SCORER,
        reason="initial adjudicated capture",
        evaluation_at=EARLY_START + timedelta(hours=2),
    )
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(hours=3))
    ruck_player = acquire(pool, ownership, scope, entry, 4, LATE_HOME, name="Ordinarily Completed Ruck")
    draft2 = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {**submission.positions, "Ruck": ruck_player.season_player_id},
        expected_revision=submission.based_on_draft_revision,
        actor=COACH,
    )
    resubmitted = lineups.submit(
        draft2.lineup_id,
        expected_draft_revision=draft2.revision,
        expected_submission_version=submission.version,
        actor=COACH,
        lock_guard=guard,
    )
    assert resubmitted.version == submission.version + 1
    assert resubmitted.positions["Ruck"] == ruck_player.season_player_id
    assert resubmitted.positions["F1"] == early.season_player_id  # the adjudicated, now-locked value survives


def test_ownership_violation_fails_atomically():
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)
    # Release the early player's ownership after the draft was saved.
    ownership.release(early.season_player_id, reason="released before adjudication")
    with pytest.raises(LineupIntegrityError, match="not currently owned"):
        service.accept_evidenced_draft(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            actor=SCORER,
            reason="attempt against a since-released player",
            evaluation_at=EARLY_START + timedelta(hours=2),
        )
    assert lineups.get_effective_submission(draft.lineup_id) is None
    assert db.execute("SELECT 1 FROM lineup_adjudication WHERE lineup_id=?", (draft.lineup_id,)).fetchone() is None


def _opening_round_scenario(year):
    db = migrated_connection()
    lifecycle, round_, entries, scope = setup_scope(db, year, year)
    lifecycle.transition(round_.bbbffl_round_id, "live")
    entry = entries[0]
    rule, nominated_player, _nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, entry, position="F1"
    )
    lineups = WeeklyLineupRepository(db)
    OpeningRoundNominationRepository(db).preload_target_lineup(
        lineups, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    return db, lifecycle, round_, entry, scope, lineups, rule, nominated_player


def test_opening_round_deferred_position_takes_precedence_over_draft_evidence():
    db, lifecycle, round_, entry, scope, lineups, rule, nominated_player = _opening_round_scenario(2830)
    draft = lineups.get_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    other = own_player(db, scope["season_id"], entry, 700100, "Other Player", afl_team_id=EARLY_HOME.team_id)
    positions = dict(draft.positions)
    positions["M1"] = other.season_player_id
    lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        positions,
        expected_revision=draft.revision,
        actor=COACH,
    )
    service = LineupAdjudicationService(db, afl_client=None)
    service.match_facts = FakeMatchFacts(ALL_MATCHES)
    _activate_early_trigger(service, round_.bbbffl_round_id)
    submission, adjudication = service.accept_evidenced_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        actor=SCORER,
        reason="deferred slot must survive adjudication",
        evaluation_at=EARLY_START + timedelta(hours=2),
    )
    assert submission.positions["F1"] == nominated_player.season_player_id
    f1_slot = next(s for s in adjudication.slots if s.position == "F1")
    assert f1_slot.evidence_status == "opening_round_deferred"


def test_mixed_invalid_proposal_fails_atomically_with_no_partial_rows():
    """`submit_adjudicated_first_submission` re-validates the complete
    proposed position map (duplicates, legal positions, ownership) exactly
    like every other submission source, atomically with its own
    adjudication provenance -- a caller cannot smuggle an invalid mixed
    proposal through by way of its own `resolve_positions` callback.
    `app.lineups.WeeklyLineupRepository.save_draft`/ordinary submission
    already prevent an *ordinary* coach draft from ever holding a
    duplicate player (see `_normalise`'s use throughout this module), so
    this exercises the domain primitive directly to prove the same
    invariant holds for this workflow's own resolved-position callback,
    not only for whatever a private draft happened to already forbid."""
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)

    def resolve_positions(conn, lineup_row):
        return {"F1": early.season_player_id, "M1": early.season_player_id}  # same player twice

    def record_adjudication(conn, version):
        raise AssertionError("must never be called once resolve_positions/_normalise rejects the proposal")

    with pytest.raises(LineupIntegrityError, match="multiple"):
        lineups.submit_adjudicated_first_submission(
            draft.lineup_id,
            actor=SCORER,
            reason="mixed/invalid proposal must fail atomically",
            source_type="scorer_late_capture",
            source_detail=None,
            resolve_positions=resolve_positions,
            record_adjudication=record_adjudication,
        )
    assert lineups.get_effective_submission(draft.lineup_id) is None
    assert db.execute("SELECT 1 FROM lineup_adjudication WHERE lineup_id=?", (draft.lineup_id,)).fetchone() is None
    assert db.execute("SELECT 1 FROM weekly_lineup_submission WHERE lineup_id=?", (draft.lineup_id,)).fetchone() is None
    assert lineups.get_effective_submission(draft.lineup_id) is None
    assert db.execute("SELECT 1 FROM lineup_adjudication WHERE lineup_id=?", (draft.lineup_id,)).fetchone() is None


# -- Resolution B: apply carry-forward fallback -----------------------------


def _carry_forward_scenario(year=2840):
    db, lifecycle, rounds, entries, scope, pool, ownership = carry_forward_context(year=year, rounds=2)
    entry = entries[0]
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME, name="Previous Round Player")
    lineups = WeeklyLineupRepository(db)
    _draft, prev_submission = submit_round(
        lineups, scope, rounds[0], entry, {"F1": early.season_player_id}, neutral_team=LATE_HOME
    )
    lifecycle.transition(rounds[1], "live")
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, rounds[1], [EARLY_MATCH_ID], key="early-1", sequence=1)
    matches = FakeMatchFacts(ALL_MATCHES)
    service = LineupAdjudicationService(db, afl_client=None)
    service.match_facts = matches
    # An unrelated private draft this entry saved for the new round -- must
    # never be merged into the carry-forward result.
    rejected = acquire(pool, ownership, scope, entry, 2, LATE_HOME, name="Rejected Draft Player")
    lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        {"M1": rejected.season_player_id},
        expected_revision=0,
        actor=COACH,
    )
    return (
        db,
        lifecycle,
        rounds[1],
        entry,
        scope,
        pool,
        ownership,
        lineups,
        matches,
        service,
        prev_submission,
        early,
        rejected,
    )


def test_valid_carry_forward_adjudication_and_rejected_draft_is_never_merged():
    (
        db,
        lifecycle,
        current_round_id,
        entry,
        scope,
        pool,
        ownership,
        lineups,
        matches,
        service,
        prev_submission,
        early,
        rejected,
    ) = _carry_forward_scenario()
    _activate_early_trigger(service, current_round_id)
    submission, adjudication = service.apply_carry_forward_fallback(
        scope["season_id"],
        scope["competition_id"],
        current_round_id,
        entry.season_entry_id,
        actor=SCORER,
        reason="Quorum rejected the late request; carry-forward applied",
        evaluation_at=EARLY_START + timedelta(hours=2),
    )
    assert submission.version == 1
    assert submission.source_type == "scorer_adjudicated_carry_forward"
    assert submission.positions["F1"] == early.season_player_id
    assert submission.positions["M1"] != rejected.season_player_id  # never the rejected draft's player
    assert adjudication.decision_type == DECISION_APPLY_CARRY_FORWARD
    assert adjudication.source_lineup_id == prev_submission.lineup_id
    assert adjudication.source_submission_version == prev_submission.version


def test_carry_forward_refused_without_activated_trigger():
    (
        db,
        lifecycle,
        current_round_id,
        entry,
        scope,
        pool,
        ownership,
        lineups,
        matches,
        service,
        prev_submission,
        early,
        rejected,
    ) = _carry_forward_scenario()
    with pytest.raises(RoundNotEligibleForAdjudicationError, match="no lockout trigger"):
        service.apply_carry_forward_fallback(
            scope["season_id"],
            scope["competition_id"],
            current_round_id,
            entry.season_entry_id,
            actor=SCORER,
            reason="no trigger activated yet",
            evaluation_at=EARLY_START - timedelta(days=2),
        )


def test_carry_forward_with_no_source_round_raises():
    db, lifecycle, rounds, entries, scope, pool, ownership = carry_forward_context(year=2850, rounds=1)
    lifecycle.transition(rounds[0], "live")
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, rounds[0], [EARLY_MATCH_ID], key="early-1", sequence=1)
    matches = FakeMatchFacts(ALL_MATCHES)
    service = LineupAdjudicationService(db, afl_client=None)
    service.match_facts = matches
    _activate_early_trigger(service, rounds[0])
    with pytest.raises(NoCarryForwardSourceError):
        service.apply_carry_forward_fallback(
            scope["season_id"],
            scope["competition_id"],
            rounds[0],
            entry.season_entry_id,
            actor=SCORER,
            reason="round 1 has no predecessor",
            evaluation_at=EARLY_START + timedelta(hours=2),
        )


# -- Audit/provenance --------------------------------------------------------


def test_adjudication_shares_correlation_id_with_submission_audit_event():
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)
    submission, adjudication = service.accept_evidenced_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        actor=SCORER,
        reason="checking correlation between adjudication and submission audit",
        evaluation_at=EARLY_START + timedelta(hours=2),
    )
    events = AuditEventRepository(db).list_events(correlation_id=adjudication.correlation_id)
    actions = {event.action for event in events}
    assert {LINEUP_SUBMITTED, LINEUP_ADJUDICATED} <= actions
    submitted_event = next(e for e in events if e.action == LINEUP_SUBMITTED)
    adjudicated_event = next(e for e in events if e.action == LINEUP_ADJUDICATED)
    assert submitted_event.entity_id == adjudicated_event.entity_id == draft.lineup_id
    assert submitted_event.correlation_id == adjudicated_event.correlation_id == adjudication.correlation_id
    assert submission.version == 1


def test_replay_operator_may_adjudicate():
    db, lifecycle, round_, entry, scope, pool, ownership, lineups, matches, draft, early, late, service = (
        _missed_submission_scenario()
    )
    _activate_early_trigger(service, round_.bbbffl_round_id)
    submission, adjudication = service.accept_evidenced_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        actor=REPLAY_OPERATOR,
        reason="Replay operator authorised for this replay season",
        evaluation_at=EARLY_START + timedelta(hours=2),
    )
    assert submission.actor_role == "replay_operator"
    assert adjudication.actor_role == "replay_operator"
