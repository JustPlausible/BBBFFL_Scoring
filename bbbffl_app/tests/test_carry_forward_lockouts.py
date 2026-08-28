"""Carry-forward must integrate with the real staged-lockout boundary
(#34) rather than bypass it: this reuses `app.lockouts.LockGuard` exactly
as an ordinary coach submission does, via
`WeeklyLineupRepository.submit_positions`'s `lock_guard` parameter -- no
lock evaluation is duplicated in `app.carry_forward` (see its module
docstring). Every scenario here mirrors an existing `tests/test_lockouts.py`
guard scenario, exercised through the carry-forward path instead of a plain
coach `submit`."""

from datetime import timedelta

import pytest

from app.audit import ActorContext
from app.carry_forward import CarryForwardService, NoCarryForwardSourceError
from app.lineups import WeeklyLineupRepository
from app.lockouts import LockedSelectionError, LockoutRepository, LockoutTriggerRepository
from tests.lineup_helpers import complete_lineup
from tests.test_carry_forward import context
from tests.test_carry_forward import submit_round as _submit_round
from tests.test_lockouts import (
    ALL_MATCHES,
    EARLY_HOME,
    EARLY_MATCH_ID,
    EARLY_START,
    LATE_HOME,
    FakeMatchFacts,
    configure_selective,
)

CARRY_FORWARD_ACTOR = ActorContext.system()


def submit_round(*args, **kwargs):
    """All unrelated slots use the scheduled late club, so these scenarios
    reach the specific staged-lockout decision they are intended to test."""
    return _submit_round(*args, neutral_team=LATE_HOME, **kwargs)


def acquire_club_player(pool, ownership, scope, entry, canonical_id, team, name=None):
    player = pool.refresh_player(
        scope["season_id"],
        canonical_id,
        name or f"Player {canonical_id}",
        afl_team_id=team.team_id,
        afl_team_name=team.name,
    )
    ownership.acquire(player.season_player_id, entry.season_entry_id)
    return player


def test_carry_forward_cannot_introduce_a_player_whose_current_round_match_is_already_locked():
    """Round 2 has never been submitted; its own trigger has already locked
    EARLY_MATCH_ID. Carrying forward round 1's F1 (an EARLY_HOME player)
    into round 2 introduces a brand-new selection into an already-locked
    match -- rejected exactly as an ordinary first-time coach submission
    would be, and nothing is persisted."""
    db, _, rounds, entries, scope, pool, ownership = context(rounds=2)
    entry = entries[0]
    early = acquire_club_player(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    submit_round(lineups, scope, rounds[0], entry, {"F1": early.season_player_id})

    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, rounds[1], [EARLY_MATCH_ID], key="early-1", sequence=1)
    matches = FakeMatchFacts(ALL_MATCHES)
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))

    with pytest.raises(LockedSelectionError, match="F1"):
        CarryForwardService(db).carry_forward(
            scope["season_id"],
            scope["competition_id"],
            rounds[1],
            entry.season_entry_id,
            expected_submission_version=0,
            actor=CARRY_FORWARD_ACTOR,
            lock_guard=guard,
        )
    assert (
        WeeklyLineupRepository(db).get_effective_submission(
            lineups.get_or_create_header(scope["season_id"], scope["competition_id"], rounds[1], entry.season_entry_id)[
                0
            ]
        )
        is None
    )


def test_carry_forward_cannot_overwrite_an_already_locked_effective_assignment():
    """Round 2 already has its own effective submission, and its F1 has
    already been observed/materialized as locked. A later carry-forward
    attempt (e.g. re-run after a scorer correction elsewhere) proposing a
    *different* player for F1 must be rejected -- carry-forward gets no
    special exemption from #34's irreversible per-position lock evidence."""
    db, _, rounds, entries, scope, pool, ownership = context(rounds=2)
    entry = entries[0]
    early = acquire_club_player(pool, ownership, scope, entry, 1, EARLY_HOME)
    other_early = acquire_club_player(pool, ownership, scope, entry, 2, EARLY_HOME, name="Other Early")
    late = acquire_club_player(pool, ownership, scope, entry, 3, LATE_HOME)
    lineups = WeeklyLineupRepository(db)
    # Round 1's source lineup proposes a *different* F1 to round 2's own.
    submit_round(lineups, scope, rounds[0], entry, {"F1": other_early.season_player_id})

    matches = FakeMatchFacts(ALL_MATCHES)
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, rounds[1], [EARLY_MATCH_ID], key="early-1", sequence=1)
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        complete_lineup(
            db, scope, entry, {"F1": early.season_player_id, "M1": late.season_player_id}, neutral_team=LATE_HOME
        ),
        expected_revision=0,
    )
    round2_first = lineups.submit(
        draft.lineup_id, expected_draft_revision=1, expected_submission_version=0, lock_guard=pre_lock_guard
    )

    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    with pytest.raises(LockedSelectionError, match="F1"):
        CarryForwardService(db).carry_forward(
            scope["season_id"],
            scope["competition_id"],
            rounds[1],
            entry.season_entry_id,
            expected_submission_version=round2_first.version,
            actor=CARRY_FORWARD_ACTOR,
            lock_guard=late_guard,
        )


def test_still_editable_positions_remain_changeable_by_carry_forward():
    """The same scenario as above, except round 1's source lineup keeps F1
    unchanged (the locked value) and only changes M1 -- an editable
    position (LATE_MATCH_ID has not started). Carry-forward must succeed
    for that unlocked position."""
    db, _, rounds, entries, scope, pool, ownership = context(rounds=2)
    entry = entries[0]
    early = acquire_club_player(pool, ownership, scope, entry, 1, EARLY_HOME)
    late_old = acquire_club_player(pool, ownership, scope, entry, 2, LATE_HOME, name="Late Old")
    late_new = acquire_club_player(pool, ownership, scope, entry, 3, LATE_HOME, name="Late New")
    lineups = WeeklyLineupRepository(db)
    submit_round(lineups, scope, rounds[0], entry, {"F1": early.season_player_id, "M1": late_new.season_player_id})

    matches = FakeMatchFacts(ALL_MATCHES)
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, rounds[1], [EARLY_MATCH_ID], key="early-1", sequence=1)
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        complete_lineup(
            db, scope, entry, {"F1": early.season_player_id, "M1": late_old.season_player_id}, neutral_team=LATE_HOME
        ),
        expected_revision=0,
    )
    round2_first = lineups.submit(
        draft.lineup_id, expected_draft_revision=1, expected_submission_version=0, lock_guard=pre_lock_guard
    )

    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    submitted, _ = CarryForwardService(db).carry_forward(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        expected_submission_version=round2_first.version,
        actor=CARRY_FORWARD_ACTOR,
        lock_guard=late_guard,
    )
    assert submitted.positions["F1"] == early.season_player_id
    assert submitted.positions["M1"] == late_new.season_player_id


def test_interchange_cannot_provide_a_carry_forward_lockout_bypass():
    """Round 2 already has an effective submission with F1 already
    observed/materialized as locked. A carry-forward attempt that keeps F1
    unchanged but introduces a brand-new same-club player into Interchange
    (a still-open position) is rejected -- Interchange provides no bypass
    via carry-forward, mirroring tests/test_lockouts.py's
    `test_interchange_cannot_bypass_a_locked_players_match`."""
    db, _, rounds, entries, scope, pool, ownership = context(rounds=2)
    entry = entries[0]
    early = acquire_club_player(pool, ownership, scope, entry, 1, EARLY_HOME)
    same_club = acquire_club_player(pool, ownership, scope, entry, 2, EARLY_HOME, name="Same Club")
    lineups = WeeklyLineupRepository(db)
    # Round 1's source lineup keeps F1 unchanged from round 2's own
    # (about-to-be-locked) selection, and additionally selects Interchange.
    submit_round(
        lineups, scope, rounds[0], entry, {"F1": early.season_player_id, "Interchange": same_club.season_player_id}
    )

    matches = FakeMatchFacts(ALL_MATCHES)
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, rounds[1], [EARLY_MATCH_ID], key="early-1", sequence=1)
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        complete_lineup(db, scope, entry, {"F1": early.season_player_id}, neutral_team=LATE_HOME),
        expected_revision=0,
    )
    round2_first = lineups.submit(
        draft.lineup_id, expected_draft_revision=1, expected_submission_version=0, lock_guard=pre_lock_guard
    )

    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    with pytest.raises(LockedSelectionError, match="Interchange"):
        CarryForwardService(db).carry_forward(
            scope["season_id"],
            scope["competition_id"],
            rounds[1],
            entry.season_entry_id,
            expected_submission_version=round2_first.version,
            actor=CARRY_FORWARD_ACTOR,
            lock_guard=late_guard,
        )


def test_indeterminate_lock_state_fails_the_carry_forward_safely():
    """A source player whose current-round match cannot be resolved at all
    (afl-api's response omits it) is indeterminate, not editable -- carry-
    forward fails closed rather than guessing, mirroring tests/
    test_lockouts.py's indeterminate-match-data scenario."""
    db, _, rounds, entries, scope, pool, ownership = context(rounds=2)
    entry = entries[0]
    early = acquire_club_player(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    submit_round(lineups, scope, rounds[0], entry, {"F1": early.season_player_id})

    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, rounds[1], [EARLY_MATCH_ID], key="early-1", sequence=1)
    # afl-api's round-2 response omits EARLY_MATCH_ID entirely.
    gapped = FakeMatchFacts([match for match in ALL_MATCHES if match.match_id != EARLY_MATCH_ID])
    guard = LockoutRepository(db).guard(match_facts=gapped, evaluation_at=EARLY_START + timedelta(minutes=2))

    with pytest.raises(LockedSelectionError, match="indeterminate"):
        CarryForwardService(db).carry_forward(
            scope["season_id"],
            scope["competition_id"],
            rounds[1],
            entry.season_entry_id,
            expected_submission_version=0,
            actor=CARRY_FORWARD_ACTOR,
            lock_guard=guard,
        )


def test_no_carry_forward_source_error_takes_precedence_over_lock_evaluation():
    """Round 1 has no predecessor at all -- `NoCarryForwardSourceError` is
    raised before any lock evaluation runs, whatever `lock_guard` was
    supplied; there is nothing to guard yet."""
    db, _, rounds, entries, scope, pool, ownership = context(rounds=1)
    entry = entries[0]
    matches = FakeMatchFacts(ALL_MATCHES)
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START)
    with pytest.raises(NoCarryForwardSourceError):
        CarryForwardService(db).carry_forward(
            scope["season_id"],
            scope["competition_id"],
            rounds[0],
            entry.season_entry_id,
            expected_submission_version=0,
            actor=CARRY_FORWARD_ACTOR,
            lock_guard=guard,
        )
