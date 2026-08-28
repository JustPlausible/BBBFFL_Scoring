"""Player-level AFL-match lockouts driven by a persisted BBBFFL round
lockout plan: commissioner/scorer-configured selective (early) and main
triggers, never "every AFL match is its own trigger".

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
    LockoutTriggerRepository,
    LockState,
    MatchResolutionError,
    TriggerAlreadyActivatedError,
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
UNCOVERED_HOME = Team(3001, "Uncovered FC")
UNCOVERED_AWAY = Team(3002, "Uncovered Opp")
STAGE_B_HOME = Team(4001, "Stage B FC")
STAGE_B_AWAY = Team(4002, "Stage B Opp")

EARLY_START = datetime(2027, 4, 3, 19, 20, tzinfo=timezone.utc)
LATE_START = datetime(2027, 4, 5, 15, 10, tzinfo=timezone.utc)
UNCOVERED_START = datetime(
    2027, 4, 3, 12, 0, tzinfo=timezone.utc
)  # earlier than EARLY_START, deliberately not configured as a trigger
STAGE_B_START = datetime(2027, 4, 4, 19, 50, tzinfo=timezone.utc)

EARLY_MATCH_ID, LATE_MATCH_ID, UNCOVERED_MATCH_ID, STAGE_B_MATCH_ID = 9001, 9002, 9003, 9004


def early_match(status="UPCOMING", start=EARLY_START):
    return Match(
        match_id=EARLY_MATCH_ID,
        home_team=EARLY_HOME,
        away_team=EARLY_AWAY,
        status=status,
        start_time_utc=start.isoformat() if start is not None else None,
    )


def late_match(status="UPCOMING", start=LATE_START):
    return Match(
        match_id=LATE_MATCH_ID,
        home_team=LATE_HOME,
        away_team=LATE_AWAY,
        status=status,
        start_time_utc=start.isoformat() if start is not None else None,
    )


def uncovered_match(status="UPCOMING", start=UNCOVERED_START):
    return Match(
        match_id=UNCOVERED_MATCH_ID,
        home_team=UNCOVERED_HOME,
        away_team=UNCOVERED_AWAY,
        status=status,
        start_time_utc=start.isoformat() if start is not None else None,
    )


def stage_b_match(status="UPCOMING", start=STAGE_B_START):
    return Match(
        match_id=STAGE_B_MATCH_ID,
        home_team=STAGE_B_HOME,
        away_team=STAGE_B_AWAY,
        status=status,
        start_time_utc=start.isoformat() if start is not None else None,
    )


ALL_MATCHES = [early_match(), late_match(), uncovered_match(), stage_b_match()]


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
# Pure decision function -- no database required. Used only to decide
# *trigger* activation now, not a per-player decision directly.
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
        Match(
            match_id=9099,
            home_team=EARLY_HOME,
            away_team=Team(3, "X"),
            status="UPCOMING",
            start_time_utc=EARLY_START.isoformat(),
        ),
    ]
    with pytest.raises(MatchResolutionError, match="ambiguous"):
        resolve_match(EARLY_HOME.team_id, duplicated)
    assert resolve_match(EARLY_AWAY.team_id, matches).match_id == EARLY_MATCH_ID


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
        scope["season_id"],
        canonical_id,
        name or f"Player {canonical_id}",
        afl_team_id=team.team_id,
        afl_team_name=team.name,
    )
    ownership.acquire(player.season_player_id, entry.season_entry_id)
    return player


def establish(lineups, round_, entry, scope, positions, *, guard=None):
    """First-ever save+submit for a fresh lineup."""
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        positions,
        expected_revision=0,
    )
    submitted = lineups.submit(
        draft.lineup_id,
        expected_draft_revision=draft.revision,
        expected_submission_version=0,
        lock_guard=guard,
    )
    return draft, submitted


def edit_draft(lineups, round_, entry, scope, lineup_id, positions, *, from_revision):
    return lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        positions,
        expected_revision=from_revision,
    )


def configure_selective(triggers, round_id, match_ids, *, key, sequence):
    return triggers.create(round_id, key, "selective", sequence, match_ids, reason=f"configure {key}")


def configure_main(triggers, round_id, match_ids, *, key="main", sequence=99):
    return triggers.create(round_id, key, "main", sequence, match_ids, reason="configure main")


# ---------------------------------------------------------------------------
# LockoutTriggerRepository: the persisted round lockout plan itself.
# ---------------------------------------------------------------------------


def test_trigger_create_and_replace_are_visible_via_list_and_get():
    db, _, round_, entries, scope, pool, ownership = context()
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    listed = triggers.list_triggers(round_.bbbffl_round_id)
    assert [t.trigger_key for t in listed] == ["early-1", "main"]
    assert listed[0].trigger_type == "selective" and listed[0].afl_match_ids == (EARLY_MATCH_ID,)
    assert listed[1].trigger_type == "main" and listed[1].afl_match_ids == (LATE_MATCH_ID,)

    replaced = triggers.replace(
        round_.bbbffl_round_id,
        "early-1",
        trigger_type="selective",
        sequence=1,
        afl_match_ids=[EARLY_MATCH_ID, STAGE_B_MATCH_ID],
        reason="AFL added a second early match",
    )
    assert replaced.revision == 2
    assert set(replaced.afl_match_ids) == {EARLY_MATCH_ID, STAGE_B_MATCH_ID}
    assert triggers.get(round_.bbbffl_round_id, "early-1") == replaced


def test_trigger_create_rejects_duplicate_key_and_empty_or_duplicate_matches():
    db, _, round_, entries, scope, pool, ownership = context()
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    with pytest.raises(LockoutIntegrityError, match="already exists"):
        configure_selective(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], key="early-1", sequence=1)
    with pytest.raises(ValueError, match="at least one"):
        configure_selective(triggers, round_.bbbffl_round_id, [], key="early-2", sequence=2)
    with pytest.raises(ValueError, match="unique"):
        configure_selective(
            triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID, EARLY_MATCH_ID], key="early-3", sequence=3
        )
    with pytest.raises(ValueError, match="trigger_type"):
        triggers.create(round_.bbbffl_round_id, "bad-type", "early", 1, [EARLY_MATCH_ID])


def test_trigger_rejects_a_second_main_and_replace_into_a_second_main():
    db, _, round_, entries, scope, pool, ownership = context()
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], key="main", sequence=99)
    with pytest.raises(LockoutIntegrityError, match="already has a main trigger"):
        configure_main(triggers, round_.bbbffl_round_id, [UNCOVERED_MATCH_ID], key="main-2", sequence=100)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    with pytest.raises(LockoutIntegrityError, match="already has a main trigger"):
        triggers.replace(
            round_.bbbffl_round_id,
            "early-1",
            trigger_type="main",
            sequence=1,
            afl_match_ids=[EARLY_MATCH_ID],
            reason="promote to main",
        )


def test_trigger_replace_requires_a_reason_and_a_known_key():
    db, _, round_, entries, scope, pool, ownership = context()
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    with pytest.raises(ValueError, match="reason"):
        triggers.replace(
            round_.bbbffl_round_id,
            "early-1",
            trigger_type="selective",
            sequence=1,
            afl_match_ids=[EARLY_MATCH_ID],
            reason="",
        )
    with pytest.raises(KeyError):
        triggers.replace(
            round_.bbbffl_round_id,
            "does-not-exist",
            trigger_type="selective",
            sequence=1,
            afl_match_ids=[EARLY_MATCH_ID],
            reason="x",
        )


def test_trigger_replace_is_rejected_once_activated_but_fine_before():
    db, _, round_, entries, scope, pool, ownership = context()
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    # Fine before activation.
    triggers.replace(
        round_.bbbffl_round_id,
        "early-1",
        trigger_type="selective",
        sequence=1,
        afl_match_ids=[STAGE_B_MATCH_ID],
        reason="swap match before it fires",
    )
    LockoutRepository(db)._materialize_round_triggers(
        round_.bbbffl_round_id,
        match_facts=FakeMatchFacts([stage_b_match()]),
        evaluation_at=STAGE_B_START + timedelta(minutes=1),
    )
    with pytest.raises(TriggerAlreadyActivatedError):
        triggers.replace(
            round_.bbbffl_round_id,
            "early-1",
            trigger_type="selective",
            sequence=1,
            afl_match_ids=[EARLY_MATCH_ID],
            reason="too late",
        )


# ---------------------------------------------------------------------------
# Required validation matrix (issue #34 + maintainer follow-up on #45).
# ---------------------------------------------------------------------------


def test_round_with_only_a_main_trigger_locks_everything_at_once():
    """1. round with only a main trigger."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID])
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    uncovered = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(
        lineups, round_, entry, scope, {"F1": early.season_player_id, "M1": uncovered.season_player_id}
    )
    lock_repo = LockoutRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)

    before = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=LATE_START - timedelta(minutes=1),
    )
    assert before.positions["F1"].state == LockState.EDITABLE
    assert before.positions["M1"].state == LockState.EDITABLE

    after = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=LATE_START,
    )
    assert after.positions["F1"].state == LockState.LOCKED
    assert after.positions["F1"].reason == "main_lockout_triggered"
    assert after.positions["M1"].state == LockState.LOCKED
    assert after.positions["M1"].reason == "main_lockout_triggered"


def test_early_trigger_plus_main_locks_progressively():
    """2. round with one early trigger plus main."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    other = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(
        lineups, round_, entry, scope, {"F1": early.season_player_id, "M1": other.season_player_id}
    )
    lock_repo = LockoutRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)

    after_early = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    assert after_early.positions["F1"].state == LockState.LOCKED
    assert after_early.positions["F1"].reason == "selective_trigger_activated"
    assert after_early.positions["M1"].state == LockState.EDITABLE

    after_main = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=LATE_START + timedelta(minutes=1),
    )
    assert after_main.positions["F1"].state == LockState.LOCKED  # unchanged, still via the early trigger
    assert after_main.positions["M1"].state == LockState.LOCKED
    assert after_main.positions["M1"].reason == "main_lockout_triggered"


def test_match_starting_before_main_but_not_configured_stays_editable():
    """3. AFL match starts before main but is not configured as an early trigger."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID])
    uncovered = acquire(pool, ownership, scope, entry, 1, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": uncovered.season_player_id})
    lock_repo = LockoutRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)

    # UNCOVERED_START is well before LATE_START (main); the match itself has
    # long since started/concluded, but it was never configured as a trigger.
    view = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=FakeMatchFacts([uncovered_match(status="CONCLUDED"), late_match()]),
        evaluation_at=LATE_START - timedelta(minutes=1),
    )
    assert view.positions["F1"].state == LockState.EDITABLE

    after_main = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=LATE_START,
    )
    assert after_main.positions["F1"].state == LockState.LOCKED
    assert after_main.positions["F1"].reason == "main_lockout_triggered"


def test_multiple_selective_stages_lock_independently_then_main_locks_the_rest():
    """4. multiple selective stages."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="stage-a", sequence=1)
    configure_selective(triggers, round_.bbbffl_round_id, [STAGE_B_MATCH_ID], key="stage-b", sequence=2)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=3)
    a_player = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    b_player = acquire(pool, ownership, scope, entry, 2, STAGE_B_HOME)
    remaining = acquire(pool, ownership, scope, entry, 3, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(
        lineups,
        round_,
        entry,
        scope,
        {"F1": a_player.season_player_id, "M1": b_player.season_player_id, "Ruck": remaining.season_player_id},
    )
    lock_repo = LockoutRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)

    only_a = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    assert only_a.positions["F1"].state == LockState.LOCKED
    assert only_a.positions["M1"].state == LockState.EDITABLE
    assert only_a.positions["Ruck"].state == LockState.EDITABLE

    a_and_b = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=STAGE_B_START + timedelta(minutes=1),
    )
    assert a_and_b.positions["F1"].state == LockState.LOCKED
    assert a_and_b.positions["M1"].state == LockState.LOCKED
    assert a_and_b.positions["Ruck"].state == LockState.EDITABLE

    all_locked = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=LATE_START + timedelta(minutes=1),
    )
    assert all_locked.positions["Ruck"].state == LockState.LOCKED
    assert all_locked.positions["Ruck"].reason == "main_lockout_triggered"


def test_pretrigger_configuration_change_moves_the_effective_boundary():
    """5. pre-trigger configuration change."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    # Wrongly configured onto STAGE_B_MATCH_ID (which has not itself
    # started yet either, so the trigger genuinely has not activated) --
    # the player's own match starting has no effect while misconfigured.
    configure_selective(triggers, round_.bbbffl_round_id, [STAGE_B_MATCH_ID], key="early-1", sequence=1)
    player = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": player.season_player_id})
    lock_repo = LockoutRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)

    before_swap = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    assert before_swap.positions["F1"].state == LockState.EDITABLE

    # Commissioner corrects the trigger to the player's actual match before
    # it has fired.
    triggers.replace(
        round_.bbbffl_round_id,
        "early-1",
        trigger_type="selective",
        sequence=1,
        afl_match_ids=[EARLY_MATCH_ID],
        reason="corrected to the right match",
    )
    after_swap = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    assert after_swap.positions["F1"].state == LockState.LOCKED
    assert after_swap.positions["F1"].reason == "selective_trigger_activated"


def test_posttrigger_configuration_change_is_rejected_and_locks_survive():
    """6. post-trigger configuration change."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    player = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": player.season_player_id})
    lock_repo = LockoutRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)

    activated = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    assert activated.positions["F1"].state == LockState.LOCKED
    assert activated.positions["F1"].irreversible is True

    with pytest.raises(TriggerAlreadyActivatedError):
        triggers.replace(
            round_.bbbffl_round_id,
            "early-1",
            trigger_type="selective",
            sequence=1,
            afl_match_ids=[STAGE_B_MATCH_ID],
            reason="attempt to move the goalposts",
        )

    # A hypothetical corrected/rescheduled view of the match itself must
    # also not unlock it -- the trigger-activation layer protects this
    # independent of the attempted (and rejected) reconfiguration above.
    corrected = FakeMatchFacts(
        [
            Match(
                match_id=EARLY_MATCH_ID,
                home_team=EARLY_HOME,
                away_team=EARLY_AWAY,
                status="UPCOMING",
                start_time_utc=(EARLY_START + timedelta(days=1)).isoformat(),
            )
        ]
    )
    still_locked = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=corrected,
        evaluation_at=EARLY_START + timedelta(minutes=2),
    )
    assert still_locked.positions["F1"].state == LockState.LOCKED
    assert still_locked.positions["F1"].irreversible is True


def test_stale_lockout_plan_revision_racing_a_submission_fails_safely():
    """7. stale lockout-plan revision racing a coach submission."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    # Configured onto STAGE_B_MATCH_ID, which has not started at any
    # evaluation instant used below -- the trigger genuinely has not
    # activated yet when it gets retargeted.
    configure_selective(triggers, round_.bbbffl_round_id, [STAGE_B_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)

    # A coach's browser loads the page while the trigger only covers a
    # different match -- their own player looks editable. Before their edit
    # reaches the server, the commissioner retargets the trigger onto the
    # coach's actual match and it fires.
    pre_change_guard = LockoutRepository(db).guard(
        match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5)
    )
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_change_guard)

    triggers.replace(
        round_.bbbffl_round_id,
        "early-1",
        trigger_type="selective",
        sequence=1,
        afl_match_ids=[EARLY_MATCH_ID],
        reason="retarget onto the real match",
    )

    other = acquire(pool, ownership, scope, entry, 2, EARLY_HOME, name="Stale Edit Replacement")
    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    draft2 = edit_draft(
        lineups, round_, entry, scope, draft.lineup_id, {"F1": other.season_player_id}, from_revision=draft.revision
    )
    with pytest.raises(LockedSelectionError):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=late_guard,
        )

    assert lineups.get_effective_submission(draft.lineup_id).positions["F1"] == early.season_player_id


def test_deterministic_replay_with_a_persisted_lockout_plan():
    """8. deterministic 2026 replay with a persisted historical lockout plan."""
    db, _, round_, entries, scope, pool, ownership = context(year=2026)
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    player = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": player.season_player_id})

    at = EARLY_START + timedelta(minutes=1)
    first = LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=FakeMatchFacts(ALL_MATCHES),
        evaluation_at=at,
    )
    second = LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=FakeMatchFacts(ALL_MATCHES),
        evaluation_at=at,
    )
    assert first.positions["F1"].state == second.positions["F1"].state == LockState.LOCKED
    assert first.positions["F1"].reason == second.positions["F1"].reason == "selective_trigger_activated"
    assert first.positions["F1"].afl_match_id == second.positions["F1"].afl_match_id == EARLY_MATCH_ID


def test_2026_and_2027_lockout_plans_remain_independently_scoped():
    """9. 2026 and 2027 plans remain season-scoped."""
    shared_db = migrated_connection()
    _, _, round2026, entries2026, scope2026, pool2026, ownership2026 = context(year=2026, db=shared_db)
    _, _, round2027, entries2027, scope2027, pool2027, ownership2027 = context(year=2027, db=shared_db)
    triggers = LockoutTriggerRepository(shared_db)
    # Only 2026's round gets a main trigger configured on the shared match ID.
    configure_main(triggers, round2026.bbbffl_round_id, [LATE_MATCH_ID])

    lineups2026 = WeeklyLineupRepository(shared_db)
    early2026 = acquire(pool2026, ownership2026, scope2026, entries2026[0], 1, LATE_HOME)
    draft2026, submitted2026 = establish(
        lineups2026, round2026, entries2026[0], scope2026, {"F1": early2026.season_player_id}
    )

    lineups2027 = WeeklyLineupRepository(shared_db)
    early2027 = acquire(pool2027, ownership2027, scope2027, entries2027[0], 1, LATE_HOME)
    draft2027, submitted2027 = establish(
        lineups2027, round2027, entries2027[0], scope2027, {"F1": early2027.season_player_id}
    )

    at = LATE_START + timedelta(minutes=1)
    view2026 = LockoutRepository(shared_db).lock_state(
        draft2026.lineup_id,
        round2026.bbbffl_round_id,
        entries2026[0].season_entry_id,
        submitted2026.positions,
        match_facts=FakeMatchFacts([late_match()]),
        evaluation_at=at,
    )
    view2027 = LockoutRepository(shared_db).lock_state(
        draft2027.lineup_id,
        round2027.bbbffl_round_id,
        entries2027[0].season_entry_id,
        submitted2027.positions,
        match_facts=FakeMatchFacts([late_match()]),
        evaluation_at=at,
    )
    assert view2026.positions["F1"].state == LockState.LOCKED
    # 2027's round has no lockout plan configured at all -- fails closed to
    # indeterminate rather than guessing either lock or unlock.
    assert view2027.positions["F1"].state == LockState.INDETERMINATE
    assert view2027.positions["F1"].reason == "lockout_plan_not_configured"


# ---------------------------------------------------------------------------
# 10. Existing per-position immutable lock evidence behaviour remains green,
# now driven by trigger coverage instead of raw per-match timing.
# ---------------------------------------------------------------------------


def _locked_context():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    late = acquire(pool, ownership, scope, entry, 2, LATE_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    draft, submitted = establish(
        lineups, round_, entry, scope, {"F1": early.season_player_id, "M1": late.season_player_id}
    )
    LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    return db, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, early, late


def test_locked_player_cannot_be_removed():
    db, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, early, late = _locked_context()
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": None, "M1": late.season_player_id},
        from_revision=draft.revision,
    )
    with pytest.raises(LockedSelectionError, match="F1"):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard,
        )


def test_locked_player_cannot_be_replaced():
    db, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, early, late = _locked_context()
    other = acquire(pool, ownership, scope, entry, 3, EARLY_HOME, name="Bench Forward")
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": other.season_player_id, "M1": late.season_player_id},
        from_revision=draft.revision,
    )
    with pytest.raises(LockedSelectionError, match="F1"):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard,
        )


def test_locked_player_cannot_be_repositioned_or_swapped():
    db, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, early, late = _locked_context()
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": late.season_player_id, "M1": early.season_player_id},
        from_revision=draft.revision,
    )
    with pytest.raises(LockedSelectionError):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard,
        )


def test_interchange_cannot_bypass_a_locked_players_match():
    """A brand-new player from an already-activated trigger's match cannot
    be introduced into a still-open position (including Interchange)."""
    db, round_, entry, scope, pool, ownership, lineups, matches, draft, submitted, early, late = _locked_context()
    same_club = acquire(pool, ownership, scope, entry, 3, EARLY_HOME, name="Same Started Club")
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": early.season_player_id, "M1": late.season_player_id, "Interchange": same_club.season_player_id},
        from_revision=draft.revision,
    )
    with pytest.raises(LockedSelectionError, match="Interchange"):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard,
        )


def test_indeterminate_due_to_missing_match_data_blocks_change_but_allows_unchanged_resubmission():
    """A player selected while their match was normally resolvable can
    become indeterminate if a later afl-api response is missing that match
    entirely (a data gap, not a status change) -- this must block *changing*
    the position (fail closed) while still allowing the coach to resubmit
    their unchanged selection."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    normal = FakeMatchFacts(ALL_MATCHES)
    guard0 = LockoutRepository(db).guard(match_facts=normal, evaluation_at=EARLY_START - timedelta(minutes=5))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=guard0)

    # A subsequent afl-api response omits EARLY_MATCH_ID entirely.
    gapped = FakeMatchFacts([late_match()])
    other = acquire(pool, ownership, scope, entry, 2, LATE_HOME, name="Blocked Replacement")
    guard = LockoutRepository(db).guard(match_facts=gapped, evaluation_at=EARLY_START + timedelta(minutes=2))
    draft2 = edit_draft(
        lineups, round_, entry, scope, draft.lineup_id, {"F1": other.season_player_id}, from_revision=draft.revision
    )
    with pytest.raises(LockedSelectionError, match="indeterminate"):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard,
        )

    draft3 = edit_draft(
        lineups, round_, entry, scope, draft.lineup_id, {"F1": early.season_player_id}, from_revision=draft2.revision
    )
    resubmitted = lineups.submit(
        draft3.lineup_id,
        expected_draft_revision=draft3.revision,
        expected_submission_version=submitted.version,
        lock_guard=guard,
    )
    assert resubmitted.positions["F1"] == early.season_player_id


def test_rejected_submission_still_durably_materializes_the_observed_lock():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_lock_guard)

    other = acquire(pool, ownership, scope, entry, 2, EARLY_HOME, name="Rejected Replacement")
    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    draft2 = edit_draft(
        lineups, round_, entry, scope, draft.lineup_id, {"F1": other.season_player_id}, from_revision=draft.revision
    )
    with pytest.raises(LockedSelectionError):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=late_guard,
        )

    rows = db.execute(
        "SELECT season_player_id, lock_reason FROM weekly_lineup_lock WHERE lineup_id=? AND position='F1'",
        (draft.lineup_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["season_player_id"] == early.season_player_id
    assert rows[0]["lock_reason"] == "selective_trigger_activated"

    trigger_rows = db.execute("SELECT afl_match_id FROM bbbffl_round_lockout_trigger_activation").fetchall()
    assert [r["afl_match_id"] for r in trigger_rows] == [EARLY_MATCH_ID]


def test_lock_state_never_materializes_evidence_for_a_non_effective_draft_selection():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    official = acquire(pool, ownership, scope, entry, 1, EARLY_HOME, name="Officially Submitted")
    draft_only = acquire(pool, ownership, scope, entry, 2, EARLY_HOME, name="Unsubmitted Draft Choice")
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": official.season_player_id})

    unsubmitted_draft = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": draft_only.season_player_id},
        from_revision=draft.revision,
    )
    view = LockoutRepository(db).lock_state(
        unsubmitted_draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        unsubmitted_draft.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    assert view.positions["F1"].state == LockState.LOCKED
    assert view.positions["F1"].irreversible is False

    rows = db.execute(
        "SELECT season_player_id FROM weekly_lineup_lock WHERE lineup_id=? AND position='F1'", (draft.lineup_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["season_player_id"] == official.season_player_id

    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    with pytest.raises(LockedSelectionError):
        lineups.submit(
            unsubmitted_draft.lineup_id,
            expected_draft_revision=unsubmitted_draft.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard,
        )


def test_unresolvable_match_is_surfaced_as_indeterminate_in_read_model():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID])
    orphan = acquire(pool, ownership, scope, entry, 1, Team(9999, "No Match Scheduled"))
    lineups = WeeklyLineupRepository(db)
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": orphan.season_player_id},
        expected_revision=0,
    )
    view = LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": orphan.season_player_id},
        match_facts=FakeMatchFacts(ALL_MATCHES),
        evaluation_at=LATE_START + timedelta(minutes=1),
    )
    # Even with main activated, a genuinely unresolvable player identity is
    # never silently swept into a lock decision.
    assert view.positions["F1"].state == LockState.INDETERMINATE
    assert "no AFL match found" in view.positions["F1"].reason


def test_lock_state_rejects_unknown_positions():
    db, _, round_, entries, scope, pool, ownership = context()
    with pytest.raises(LockoutIntegrityError, match="unknown scoring positions"):
        LockoutRepository(db).lock_state(
            "some-lineup",
            round_.bbbffl_round_id,
            entries[0].season_entry_id,
            {"NotAPosition": None},
            match_facts=FakeMatchFacts([]),
            evaluation_at=EARLY_START,
        )


def test_guard_transition_runs_inside_the_submit_transaction_and_rolls_back_together():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    other = acquire(pool, ownership, scope, entry, 2, LATE_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_lock_guard)

    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": None, "M1": other.season_player_id},
        from_revision=draft.revision,
    )
    with pytest.raises(LockedSelectionError):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=late_guard,
        )

    effective = lineups.get_effective_submission(draft.lineup_id)
    assert effective.version == submitted.version
    assert effective.positions["M1"] is None
