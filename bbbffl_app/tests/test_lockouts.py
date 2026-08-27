"""Player-level AFL-match lockouts: pure decision function, persisted
irreversible evidence, and integration with #33's submitted-version model.

Every evaluation below supplies an explicit `evaluation_at` -- no test
sleeps, waits on wall-clock time, or talks to a live AFL API.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.afl_client import Match, Team
from app.lineups import WeeklyLineupRepository
from app.lockouts import (
    LockedSelectionError,
    LockoutIntegrityError,
    LockoutRepository,
    LockState,
    MatchResolutionError,
    evaluate_match_lock,
    resolve_match,
)
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import operational

EARLY_HOME = Team(1001, "Early FC")
EARLY_AWAY = Team(1002, "Early Opp")
LATE_HOME = Team(2001, "Late FC")
LATE_AWAY = Team(2002, "Late Opp")

EARLY_START = datetime(2027, 4, 3, 19, 20, tzinfo=timezone.utc)
LATE_START = datetime(2027, 4, 5, 15, 10, tzinfo=timezone.utc)


def early_match(status="UPCOMING", start=EARLY_START):
    return Match(
        match_id=9001,
        home_team=EARLY_HOME,
        away_team=EARLY_AWAY,
        status=status,
        start_time_utc=start.isoformat() if start is not None else None,
    )


def late_match(status="UPCOMING", start=LATE_START):
    return Match(
        match_id=9002,
        home_team=LATE_HOME,
        away_team=LATE_AWAY,
        status=status,
        start_time_utc=start.isoformat() if start is not None else None,
    )


class FakeMatchFacts:
    """Duck-typed MatchFactsProvider returning a fixed, caller-controlled
    match list -- stands in for RoundMatchFactsProvider without touching
    app.round_mapping or a real afl-api client."""

    def __init__(self, matches):
        self.matches = list(matches)
        self.calls = 0

    def matches_for(self, bbbffl_round_id):
        self.calls += 1
        return self.matches


# ---------------------------------------------------------------------------
# Pure decision function -- no database required.
# ---------------------------------------------------------------------------


def test_editable_immediately_before_match_start():
    state, reason = evaluate_match_lock(early_match(), EARLY_START - timedelta(seconds=1))
    assert state == LockState.EDITABLE
    assert reason == "not_yet_started"


def test_locked_exactly_at_the_defined_boundary():
    state, reason = evaluate_match_lock(early_match(), EARLY_START)
    assert state == LockState.LOCKED
    assert reason == "match_time_reached"


def test_locked_immediately_after_start():
    state, _ = evaluate_match_lock(early_match(), EARLY_START + timedelta(seconds=1))
    assert state == LockState.LOCKED


def test_live_postgame_and_concluded_status_lock_regardless_of_time():
    future = EARLY_START + timedelta(days=1)
    live_state, live_reason = evaluate_match_lock(early_match(status="LIVE"), EARLY_START - timedelta(hours=1))
    postgame_state, postgame_reason = evaluate_match_lock(early_match(status="POSTGAME"), future)
    concluded_state, concluded_reason = evaluate_match_lock(early_match(status="CONCLUDED"), future)
    assert (live_state, postgame_state, concluded_state) == (LockState.LOCKED,) * 3
    # POSTGAME and CONCLUDED must never collapse into the same reason.
    assert {live_reason, postgame_reason, concluded_reason} == {
        "match_status_live",
        "match_status_postgame",
        "match_status_completed",
    }


def test_unusual_status_is_indeterminate_not_guessed_lock_or_unlock():
    for unusual in ("POSTPONED", "ABANDONED", "WASHED_OUT"):
        state, reason = evaluate_match_lock(early_match(status=unusual), EARLY_START + timedelta(hours=1))
        assert state == LockState.INDETERMINATE
        assert reason == f"unrecognized_status:{unusual}"


def test_upcoming_without_a_scheduled_start_is_indeterminate():
    state, reason = evaluate_match_lock(early_match(start=None), EARLY_START)
    assert state == LockState.INDETERMINATE
    assert reason == "missing_scheduled_start_time"


def test_resolve_match_fails_explicitly_rather_than_guessing():
    matches = [early_match(), late_match()]
    with pytest.raises(MatchResolutionError, match="no known AFL club"):
        resolve_match(None, matches)
    with pytest.raises(MatchResolutionError, match="no AFL match found"):
        resolve_match(9999, matches)
    duplicated = [
        early_match(),
        Match(match_id=9003, home_team=EARLY_HOME, away_team=Team(3, "X"), status="UPCOMING", start_time_utc=EARLY_START.isoformat()),
    ]
    with pytest.raises(MatchResolutionError, match="ambiguous"):
        resolve_match(EARLY_HOME.team_id, duplicated)
    assert resolve_match(EARLY_AWAY.team_id, matches).match_id == 9001


# ---------------------------------------------------------------------------
# Database fixtures shared by the persisted/service-level tests below.
# ---------------------------------------------------------------------------


def context(year=2027, squad_limit=10, db=None):
    db = db or migrated_connection()
    lifecycle, round_, entries = operational(db, year, year)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    pool, ownership = PlayerPoolRepository(db), OwnershipRepository(db)
    ownership.configure_squad_limit(scope["season_id"], squad_limit)
    return db, lifecycle, round_, entries, scope, pool, ownership


def acquire(pool, ownership, scope, entry, canonical_id, team, name=None):
    player = pool.refresh_player(
        scope["season_id"], canonical_id, name or f"Player {canonical_id}",
        afl_team_id=team.team_id, afl_team_name=team.name,
    )
    ownership.acquire(player.season_player_id, entry.season_entry_id)
    return player


def establish(lineups, round_, entry, scope, positions, *, guard=None):
    """First-ever save+submit for a fresh lineup -- always evaluated as if
    prepared before any relevant lock boundary, matching ordinary coach
    usage. `guard` may still be supplied (e.g. with an evaluation instant
    safely before every match's start) to prove it is a genuine no-op."""
    draft = lineups.save_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id,
        positions, expected_revision=0,
    )
    submitted = lineups.submit(
        draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0, lock_guard=guard,
    )
    return draft, submitted


def edit_draft(lineups, round_, entry, scope, lineup_id, positions, *, from_revision):
    return lineups.save_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id,
        positions, expected_revision=from_revision,
    )


# ---------------------------------------------------------------------------
# Multi-match lineup: only started matches lock.
# ---------------------------------------------------------------------------


def test_lineup_spanning_two_matches_locks_only_the_started_one():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    late = acquire(pool, ownership, scope, entry, 2, LATE_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts([early_match(), late_match()])
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id, "M1": late.season_player_id})

    view = LockoutRepository(db).lock_state(
        draft.lineup_id, round_.bbbffl_round_id, entry.season_entry_id, submitted.positions,
        match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=5),
    )
    assert view.positions["F1"].state == LockState.LOCKED
    assert view.positions["M1"].state == LockState.EDITABLE

    # The still-editable position can be changed; the frozen one is retained.
    late2 = acquire(pool, ownership, scope, entry, 3, LATE_HOME)
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=6))
    draft2 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": early.season_player_id, "M1": late2.season_player_id}, from_revision=draft.revision)
    resubmitted = lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=submitted.version, lock_guard=guard)
    assert resubmitted.positions["F1"] == early.season_player_id
    assert resubmitted.positions["M1"] == late2.season_player_id


# ---------------------------------------------------------------------------
# Ordinary coach mutations must be rejected once a position is locked.
# ---------------------------------------------------------------------------


def _locked_context():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    late = acquire(pool, ownership, scope, entry, 2, LATE_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts([early_match(), late_match()])
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id, "M1": late.season_player_id})
    # A coach/scorer read of the round once the match has genuinely started
    # is what durably materializes the lock -- exercised explicitly here so
    # later assertions do not depend on incidental evaluation timing.
    LockoutRepository(db).lock_state(
        draft.lineup_id, round_.bbbffl_round_id, entry.season_entry_id, submitted.positions,
        match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    return db, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, early, late


def test_locked_player_cannot_be_removed():
    db, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, early, late = _locked_context()
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    draft2 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": None, "M1": late.season_player_id}, from_revision=draft.revision)
    with pytest.raises(LockedSelectionError, match="F1"):
        lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=submitted.version, lock_guard=guard)


def test_locked_player_cannot_be_replaced():
    db, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, early, late = _locked_context()
    other = acquire(pool, ownership, scope, entry, 3, EARLY_HOME, name="Bench Forward")
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    draft2 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": other.season_player_id, "M1": late.season_player_id}, from_revision=draft.revision)
    with pytest.raises(LockedSelectionError, match="F1"):
        lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=submitted.version, lock_guard=guard)


def test_locked_player_cannot_be_repositioned_or_swapped():
    db, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, early, late = _locked_context()
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    # Attempt to swap the locked F1 player with the still-editable M1 player.
    draft2 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": late.season_player_id, "M1": early.season_player_id}, from_revision=draft.revision)
    with pytest.raises(LockedSelectionError):
        lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=submitted.version, lock_guard=guard)


def test_interchange_cannot_bypass_a_locked_players_match():
    """A brand-new player from an already-started club cannot be introduced
    into a still-open position (including Interchange) -- that would let
    the coach dodge the lock by routing around the frozen position."""
    db, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, early, late = _locked_context()
    same_club = acquire(pool, ownership, scope, entry, 3, EARLY_HOME, name="Same Started Club")
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    draft2 = edit_draft(
        lineups, round_, entry, scope, draft.lineup_id,
        {"F1": early.season_player_id, "M1": late.season_player_id, "Interchange": same_club.season_player_id},
        from_revision=draft.revision,
    )
    with pytest.raises(LockedSelectionError, match="Interchange"):
        lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=submitted.version, lock_guard=guard)


# ---------------------------------------------------------------------------
# Schedule/status changes.
# ---------------------------------------------------------------------------


def test_future_start_time_reschedule_before_lock_updates_the_boundary():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    rescheduled_start = EARLY_START + timedelta(hours=2)
    matches = FakeMatchFacts([early_match(start=rescheduled_start)])
    # Both the initial submission and the first edit happen after the
    # *original* scheduled time but before the newly-rescheduled one --
    # still editable because the corrected schedule, not the stale one,
    # governs.
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=30))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=guard)
    assert submitted.positions["F1"] == early.season_player_id

    other = acquire(pool, ownership, scope, entry, 2, EARLY_HOME, name="Replacement")
    guard2 = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=45))
    draft2 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": other.season_player_id}, from_revision=draft.revision)
    resubmitted = lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=submitted.version, lock_guard=guard2)
    assert resubmitted.positions["F1"] == other.season_player_id

    # Once the *rescheduled* time is reached, the position locks.
    another = acquire(pool, ownership, scope, entry, 3, EARLY_HOME, name="Too Late")
    guard3 = LockoutRepository(db).guard(match_facts=matches, evaluation_at=rescheduled_start)
    draft3 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": another.season_player_id}, from_revision=draft2.revision)
    with pytest.raises(LockedSelectionError):
        lineups.submit(draft3.lineup_id, expected_draft_revision=draft3.revision, expected_submission_version=resubmitted.version, lock_guard=guard3)


def test_upstream_correction_after_legitimate_lock_does_not_silently_unlock_history():
    db, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, early, late = _locked_context()
    lock_repo = LockoutRepository(db)
    # A subsequent upstream "correction" moves the match's scheduled start
    # into the future and reports it as UPCOMING again. Live recomputation
    # with these corrected facts alone would say editable -- but the
    # earlier lock has already been durably observed and recorded above.
    corrected = FakeMatchFacts([
        Match(match_id=9001, home_team=EARLY_HOME, away_team=EARLY_AWAY, status="UPCOMING", start_time_utc=(EARLY_START + timedelta(days=1)).isoformat()),
        late_match(),
    ])
    other = acquire(pool, ownership, scope, entry, 3, EARLY_HOME, name="Should Stay Locked Out")
    guard = lock_repo.guard(match_facts=corrected, evaluation_at=EARLY_START + timedelta(minutes=10))
    draft2 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": other.season_player_id, "M1": late.season_player_id}, from_revision=draft.revision)
    with pytest.raises(LockedSelectionError):
        lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=submitted.version, lock_guard=guard)

    view = lock_repo.lock_state(
        draft.lineup_id, round_.bbbffl_round_id, entry.season_entry_id, submitted.positions,
        match_facts=corrected, evaluation_at=EARLY_START + timedelta(minutes=10),
    )
    assert view.positions["F1"].state == LockState.LOCKED
    assert view.positions["F1"].irreversible is True


def test_indeterminate_position_blocks_change_but_allows_unchanged_resubmission():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    normal = FakeMatchFacts([early_match()])
    pre_lock_guard = LockoutRepository(db).guard(match_facts=normal, evaluation_at=EARLY_START - timedelta(minutes=5))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_lock_guard)

    unusual = FakeMatchFacts([early_match(status="POSTPONED")])
    other = acquire(pool, ownership, scope, entry, 2, EARLY_HOME, name="Blocked Replacement")
    guard2 = LockoutRepository(db).guard(match_facts=unusual, evaluation_at=EARLY_START + timedelta(minutes=2))
    draft2 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": other.season_player_id}, from_revision=draft.revision)
    with pytest.raises(LockedSelectionError, match="indeterminate"):
        lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=submitted.version, lock_guard=guard2)

    # Resubmitting the *same* selection is safe -- nothing about it changes.
    guard3 = LockoutRepository(db).guard(match_facts=unusual, evaluation_at=EARLY_START + timedelta(minutes=3))
    draft3 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": early.season_player_id}, from_revision=draft2.revision)
    resubmitted = lineups.submit(draft3.lineup_id, expected_draft_revision=draft3.revision, expected_submission_version=submitted.version, lock_guard=guard3)
    assert resubmitted.positions["F1"] == early.season_player_id


# ---------------------------------------------------------------------------
# Concurrency: an edit prepared against stale lock information must not
# silently mutate a player already locked at the authoritative decision
# point. This exercises the invariant single-process; see
# test_lockouts_concurrency.py for the real multi-threaded PostgreSQL race.
# ---------------------------------------------------------------------------


def test_stale_edit_racing_the_lock_boundary_fails_safely():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts([early_match()])
    # Prepared (in the coach's browser, conceptually) while the match had
    # not yet started.
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_lock_guard)

    # The edit itself (same draft/submission revision numbers a genuinely
    # pre-lock attempt would have used) only reaches the server once
    # authoritative evaluation time has advanced past the match's start.
    other = acquire(pool, ownership, scope, entry, 2, EARLY_HOME, name="Too Late Swap")
    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    draft2 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": other.season_player_id}, from_revision=draft.revision)
    with pytest.raises(LockedSelectionError):
        lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=submitted.version, lock_guard=late_guard)

    # The authoritative state was never mutated by the rejected attempt.
    assert lineups.get_effective_submission(draft.lineup_id).positions["F1"] == early.season_player_id


def test_guard_transition_runs_inside_the_submit_transaction_and_rolls_back_together():
    """A rejection from lock_guard must abort the whole submission, not just
    skip the guard -- proves the integration point is transactional."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    other = acquire(pool, ownership, scope, entry, 2, LATE_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts([early_match()])
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_lock_guard)

    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    draft2 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": None, "M1": other.season_player_id}, from_revision=draft.revision)
    with pytest.raises(LockedSelectionError):
        lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=submitted.version, lock_guard=late_guard)

    # M1 must not have been written either, even though it was not itself
    # locked -- the whole transaction rolled back.
    effective = lineups.get_effective_submission(draft.lineup_id)
    assert effective.version == submitted.version
    assert effective.positions["M1"] is None


# ---------------------------------------------------------------------------
# Deterministic replay and season isolation.
# ---------------------------------------------------------------------------


def test_replay_is_deterministic_given_identical_inputs():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id})

    at = EARLY_START + timedelta(minutes=1)
    first = LockoutRepository(db).lock_state(
        draft.lineup_id, round_.bbbffl_round_id, entry.season_entry_id, submitted.positions,
        match_facts=FakeMatchFacts([early_match()]), evaluation_at=at,
    )
    second = LockoutRepository(db).lock_state(
        draft.lineup_id, round_.bbbffl_round_id, entry.season_entry_id, submitted.positions,
        match_facts=FakeMatchFacts([early_match()]), evaluation_at=at,
    )
    assert first.positions["F1"].state == second.positions["F1"].state == LockState.LOCKED
    assert first.positions["F1"].reason == second.positions["F1"].reason
    assert first.positions["F1"].afl_match_id == second.positions["F1"].afl_match_id


def test_2026_and_2027_lockout_state_remain_independently_scoped():
    # One shared database: a separate database per season would trivially
    # avoid collisions, so this proves isolation actually comes from the
    # (already season-scoped) lineup identity, not accidental separation.
    shared_db = migrated_connection()
    _, _, round2026, entries2026, scope2026, pool2026, ownership2026 = context(year=2026, db=shared_db)
    lineups2026 = WeeklyLineupRepository(shared_db)
    early2026 = acquire(pool2026, ownership2026, scope2026, entries2026[0], 1, EARLY_HOME)
    draft2026, submitted2026 = establish(lineups2026, round2026, entries2026[0], scope2026, {"F1": early2026.season_player_id})

    _, _, round2027, entries2027, scope2027, pool2027, ownership2027 = context(year=2027, db=shared_db)
    lineups2027 = WeeklyLineupRepository(shared_db)
    early2027 = acquire(pool2027, ownership2027, scope2027, entries2027[0], 1, EARLY_HOME)
    draft2027, submitted2027 = establish(lineups2027, round2027, entries2027[0], scope2027, {"F1": early2027.season_player_id})

    assert draft2026.season_id != draft2027.season_id
    # 2026's evaluation instant is after the match started; 2027's is not.
    view2026 = LockoutRepository(shared_db).lock_state(
        draft2026.lineup_id, round2026.bbbffl_round_id, entries2026[0].season_entry_id, submitted2026.positions,
        match_facts=FakeMatchFacts([early_match()]), evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    view2027 = LockoutRepository(shared_db).lock_state(
        draft2027.lineup_id, round2027.bbbffl_round_id, entries2027[0].season_entry_id, submitted2027.positions,
        match_facts=FakeMatchFacts([early_match()]), evaluation_at=EARLY_START - timedelta(minutes=1),
    )
    assert view2026.positions["F1"].state == LockState.LOCKED
    assert view2027.positions["F1"].state == LockState.EDITABLE


def test_lock_state_rejects_unknown_positions():
    db, _, round_, entries, scope, pool, ownership = context()
    with pytest.raises(LockoutIntegrityError, match="unknown scoring positions"):
        LockoutRepository(db).lock_state(
            "some-lineup", round_.bbbffl_round_id, entries[0].season_entry_id, {"NotAPosition": None},
            match_facts=FakeMatchFacts([]), evaluation_at=EARLY_START,
        )


def test_unresolvable_match_is_surfaced_as_indeterminate_in_read_model():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    orphan = acquire(pool, ownership, scope, entry, 1, Team(9999, "No Match Scheduled"))
    lineups = WeeklyLineupRepository(db)
    draft = lineups.save_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id,
        {"F1": orphan.season_player_id}, expected_revision=0,
    )
    view = LockoutRepository(db).lock_state(
        draft.lineup_id, round_.bbbffl_round_id, entry.season_entry_id, {"F1": orphan.season_player_id},
        match_facts=FakeMatchFacts([early_match(), late_match()]), evaluation_at=EARLY_START,
    )
    assert view.positions["F1"].state == LockState.INDETERMINATE
    assert "no AFL match found" in view.positions["F1"].reason


# ---------------------------------------------------------------------------
# Durability: an observation must survive independently of whatever mutation
# prompted it, and must only ever come from the effective submission.
# ---------------------------------------------------------------------------


def test_rejected_submission_still_durably_materializes_the_observed_lock():
    """A submission attempt that discovers a lock and is then rejected for
    it must not lose that observation: submit's own transaction rolls back,
    but the lock evidence -- written by a separate, already-committed
    materialize() step -- must remain."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts([early_match()])
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_lock_guard)

    other = acquire(pool, ownership, scope, entry, 2, EARLY_HOME, name="Rejected Replacement")
    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    draft2 = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": other.season_player_id}, from_revision=draft.revision)
    with pytest.raises(LockedSelectionError):
        lineups.submit(draft2.lineup_id, expected_draft_revision=draft2.revision, expected_submission_version=submitted.version, lock_guard=late_guard)

    # The rejected attempt's own writes (a new submission version) rolled
    # back, but the lock it discovered along the way did not.
    rows = db.execute("SELECT season_player_id, lock_reason FROM weekly_lineup_lock WHERE lineup_id=? AND position='F1'", (draft.lineup_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["season_player_id"] == early.season_player_id
    assert rows[0]["lock_reason"] == "match_time_reached"

    # A later upstream "correction" reporting the match as freshly
    # rescheduled/UPCOMING must not be able to unlock it now that it has
    # been observed -- proving the materialize-before-transaction ordering
    # actually delivers the irreversibility guarantee end to end.
    corrected = FakeMatchFacts([Match(match_id=9001, home_team=EARLY_HOME, away_team=EARLY_AWAY, status="UPCOMING", start_time_utc=(EARLY_START + timedelta(days=1)).isoformat())])
    view = LockoutRepository(db).lock_state(
        draft.lineup_id, round_.bbbffl_round_id, entry.season_entry_id, {"F1": early.season_player_id},
        match_facts=corrected, evaluation_at=EARLY_START + timedelta(minutes=2),
    )
    assert view.positions["F1"].state == LockState.LOCKED
    assert view.positions["F1"].irreversible is True


def test_lock_state_never_materializes_evidence_for_a_non_effective_draft_selection():
    """`lock_state` may be called with an unsubmitted draft's positions (its
    docstring says so explicitly), but must never let a draft player occupy
    the one immutable evidence slot ahead of whoever is actually, officially
    selected there."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    official = acquire(pool, ownership, scope, entry, 1, EARLY_HOME, name="Officially Submitted")
    draft_only = acquire(pool, ownership, scope, entry, 2, EARLY_HOME, name="Unsubmitted Draft Choice")
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts([early_match()])
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": official.season_player_id})

    # The coach has since started editing a *draft* that swaps F1 to a
    # different player, without submitting it. A dashboard read using the
    # draft's (not the submission's) positions must not corrupt evidence.
    unsubmitted_draft = edit_draft(lineups, round_, entry, scope, draft.lineup_id, {"F1": draft_only.season_player_id}, from_revision=draft.revision)
    view = LockoutRepository(db).lock_state(
        unsubmitted_draft.lineup_id, round_.bbbffl_round_id, entry.season_entry_id, unsubmitted_draft.positions,
        match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    # Informational: the draft's own player is reported as locked (their
    # match has started too), but this must not be durably recorded.
    assert view.positions["F1"].state == LockState.LOCKED
    assert view.positions["F1"].irreversible is False

    rows = db.execute("SELECT season_player_id FROM weekly_lineup_lock WHERE lineup_id=? AND position='F1'", (draft.lineup_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["season_player_id"] == official.season_player_id

    # A subsequent legitimate submission attempt to actually change F1 away
    # from the officially-submitted (and now locked) player is still
    # correctly rejected.
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    with pytest.raises(LockedSelectionError):
        lineups.submit(unsubmitted_draft.lineup_id, expected_draft_revision=unsubmitted_draft.revision, expected_submission_version=submitted.version, lock_guard=guard)
