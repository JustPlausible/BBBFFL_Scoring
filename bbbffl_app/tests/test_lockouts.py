"""Player-level AFL-match lockouts driven by a persisted BBBFFL round
lockout plan: commissioner/scorer-configured selective (early) and main
triggers, never "every AFL match is its own trigger".

Every evaluation below supplies an explicit `evaluation_at` -- no test
sleeps, waits on wall-clock time, or talks to a live AFL API.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.afl_client import Match, Team
from app.lineups import WeeklyLineupRepository
from app.lockouts import (
    InvalidSelectionError,
    LockedSelectionError,
    LockoutIntegrityError,
    LockoutRepository,
    LockoutTriggerRepository,
    LockState,
    MatchResolutionError,
    NoScheduledMatchError,
    RoundMatchFactsProvider,
    TriggerAlreadyActivatedError,
    evaluate_match_lock,
    resolve_byes,
    resolve_match,
)
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.round_mapping import RoundMappingRepository
from tests import afl_evidence
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
# issue #185: a club deliberately absent from every match in ALL_MATCHES --
# i.e. this round's *complete* fetched match list positively does not
# include it -- models a genuine AFL bye (not merely a missing-data gap).
BYE_TEAM = Team(5001, "Bye FC")

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
    app.round_mapping or a real afl-api client.

    `byes` (issue #185), when given, is this round's *positively confirmed*
    AFL bye club ids -- exactly what `RoundMatchFactsProvider.byes_for`
    supplies in production (afl-api's own round-bye metadata). Defaults to
    `None`: every pre-existing test that constructs `FakeMatchFacts(matches)`
    without it gets no bye confirmation at all (`resolve_byes` returns
    `None`), so a club merely absent from `matches` stays the ordinary
    unresolved/`INDETERMINATE` case exactly as before this issue -- never
    silently reclassified as a safe, editable bye."""

    def __init__(self, matches, byes=None):
        self.matches = list(matches)
        self.calls = 0
        self._byes = byes

    def byes_for(self, bbbffl_round_id):
        return self._byes

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
    # issue #185: a team with no match in `matches` is a known, deterministic
    # bye -- raised as the more specific `NoScheduledMatchError` (still a
    # `MatchResolutionError`, so this generic `pytest.raises` still matches),
    # with a human-readable primary message and the raw team id retained for
    # diagnostics. See test_resolve_match_bye_is_deterministic_not_indeterminate
    # below for the dedicated coverage.
    with pytest.raises(MatchResolutionError, match="cannot be selected because AFL team 9999"):
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


def test_resolve_match_bye_is_a_distinct_deterministic_error_with_a_human_readable_message():
    """issue #185: `NoScheduledMatchError` is raised (not the base
    `MatchResolutionError`) when a team simply has no match this round --
    deterministic evidence of an AFL bye, never merely unresolved data. The
    primary message names the human-readable club when the caller supplies
    one (never a hard-coded lookup table here -- see `_evaluate_position`,
    which sources it from `season_player_pool.afl_team_name`), falling back
    to the raw provider id only when no name was available; either way the
    raw id remains on the exception for diagnostics/logging."""
    matches = [early_match(), late_match()]
    with pytest.raises(NoScheduledMatchError) as excinfo:
        resolve_match(9999, matches, afl_team_name="Adelaide Crows")
    assert (
        str(excinfo.value) == "This player cannot be selected because Adelaide Crows have no AFL match in this round."
    )
    assert excinfo.value.afl_team_id == 9999
    assert excinfo.value.afl_team_name == "Adelaide Crows"
    assert isinstance(excinfo.value, MatchResolutionError)  # every existing `except MatchResolutionError` still works

    with pytest.raises(NoScheduledMatchError) as no_name:
        resolve_match(9999, matches)
    assert str(no_name.value) == "This player cannot be selected because AFL team 9999 have no AFL match in this round."
    assert no_name.value.afl_team_name is None


# ---------------------------------------------------------------------------
# Database fixtures shared by the persisted/service-level tests below.
# ---------------------------------------------------------------------------


def context(year=2027, squad_limit=10, db=None, afl_round=None):
    db = db or migrated_connection()
    lifecycle, round_, entries = operational(db, year, afl_round if afl_round is not None else year)
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


# ---------------------------------------------------------------------------
# Issue #98: deliberately vacant positions never invent a lock/selection.
# ---------------------------------------------------------------------------


def test_vacant_position_is_fillable_before_its_boundary_and_locked_authoritative_at_main():
    """A deliberate partial submission (F1 named, everything else vacant):
    an unlocked vacancy can still be filled and resubmitted before its own
    boundary; a locked player survives that resubmission unchanged; a
    position that stays vacant through only a selective activation is never
    invented into a selection -- it simply stays vacant, reported
    `editable`/`"empty"`; but once Main itself has activated, that same
    vacancy becomes immutable too (issue #155) -- reported `locked`/
    `"main_lockout_triggered"`, never a fabricated player-level lock, and
    never presented as still-editable."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID])
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    uncovered = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    lock_repo = LockoutRepository(db)

    # Partial initial submission: only F1 is named; M1 and M2 are
    # deliberate vacancies (never fabricated -- see issue #98).
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id})
    assert submitted.positions["M1"] is None and submitted.positions["M2"] is None

    before = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START - timedelta(minutes=1),
    )
    assert before.positions["M1"].state == LockState.EDITABLE and before.positions["M1"].reason == "empty"

    # Selective A activates: F1 locks. M1/M2 remain vacant/editable -- there
    # is no player, hence no match, for any trigger to resolve or lock.
    guard_a = lock_repo.guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    after_a = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    assert after_a.positions["F1"].state == LockState.LOCKED
    assert after_a.positions["M1"].state == LockState.EDITABLE and after_a.positions["M1"].reason == "empty"

    # Fill the still-unlocked M1 vacancy and resubmit before its own
    # (uncovered) match ever reaches boundary -- allowed, and F1 stays
    # exactly as locked.
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": early.season_player_id, "M1": uncovered.season_player_id},
        from_revision=draft.revision,
    )
    resubmitted = lineups.submit(
        draft2.lineup_id,
        expected_draft_revision=draft2.revision,
        expected_submission_version=submitted.version,
        lock_guard=guard_a,
    )
    assert resubmitted.positions["F1"] == early.season_player_id
    assert resubmitted.positions["M1"] == uncovered.season_player_id
    assert resubmitted.positions["M2"] is None

    # Main activates. The still-vacant M2 is never inferred into a
    # selection -- no player, no AFL match, nothing invented -- but it is no
    # longer presented as editable either: Main's own activation is now the
    # authoritative, already-durable reason the vacancy itself is immutable
    # (issue #155), reported without ever writing fabricated player-level
    # evidence to `weekly_lineup_lock` (irreversible stays False -- there is
    # no such row and never will be for this position).
    guard_main = lock_repo.guard(match_facts=matches, evaluation_at=LATE_START)
    after_main = lock_repo.lock_state(
        draft2.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        resubmitted.positions,
        match_facts=matches,
        evaluation_at=LATE_START,
    )
    assert after_main.positions["F1"].state == LockState.LOCKED
    assert after_main.positions["M1"].state == LockState.LOCKED
    assert after_main.positions["M1"].reason == "main_lockout_triggered"
    assert after_main.positions["M2"].state == LockState.LOCKED
    assert after_main.positions["M2"].reason == "main_lockout_triggered"
    assert after_main.positions["M2"].season_player_id is None
    assert after_main.positions["M2"].irreversible is False

    # ...and Main lockout still refuses to let a *new* player be introduced
    # into that vacancy (or anywhere else): resubmitting it unchanged
    # (still vacant) succeeds, resubmitting it with a newly-named player
    # does not.
    draft3 = edit_draft(
        lineups, round_, entry, scope, draft2.lineup_id, resubmitted.positions, from_revision=draft2.revision
    )
    unchanged = lineups.submit(
        draft3.lineup_id,
        expected_draft_revision=draft3.revision,
        expected_submission_version=resubmitted.version,
        lock_guard=guard_main,
    )
    assert unchanged.positions["M2"] is None
    assert (
        unchanged.positions["F1"] == early.season_player_id and unchanged.positions["M1"] == uncovered.season_player_id
    )

    late_filler = acquire(pool, ownership, scope, entry, 3, LATE_HOME, name="Post-Main Filler")
    draft4 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft3.lineup_id,
        {**unchanged.positions, "M2": late_filler.season_player_id},
        from_revision=draft3.revision,
    )
    with pytest.raises(LockedSelectionError, match="M2"):
        lineups.submit(
            draft4.lineup_id,
            expected_draft_revision=draft4.revision,
            expected_submission_version=unchanged.version,
            lock_guard=guard_main,
        )


# ---------------------------------------------------------------------------
# Issue #155: main lockout must itself make an authoritative vacancy
# immutable, without ever fabricating/persisting player-level evidence for
# an empty position.
# ---------------------------------------------------------------------------


def test_vacancy_locks_at_main_without_ever_persisting_fabricated_player_evidence():
    """Dedicated issue #155 coverage, independent of the broader progressive
    scenario above: an intentionally vacant position (Interchange, never
    named) read before the main trigger, after only an unrelated selective
    trigger, and after main itself activates -- and, throughout, direct
    proof that `weekly_lineup_lock` (whose `season_player_id` column is
    NOT NULL -- migration 0012) never gains a row for this position, in
    any of those three states."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID])
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    lock_repo = LockoutRepository(db)

    def vacancy_rows(lineup_id):
        return db.execute(
            "SELECT 1 FROM weekly_lineup_lock WHERE lineup_id=? AND position='Interchange'", (lineup_id,)
        ).fetchall()

    # F1 is named (and will become selectively locked); Interchange is a
    # deliberate, never-named vacancy throughout.
    pre_guard = lock_repo.guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=1))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_guard)
    assert submitted.positions["Interchange"] is None

    # 1. Before any trigger: an ordinary open vacancy.
    before = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START - timedelta(minutes=1),
    )
    assert before.positions["Interchange"].state == LockState.EDITABLE
    assert before.positions["Interchange"].reason == "empty"
    assert vacancy_rows(draft.lineup_id) == []

    # 2. Only the unrelated selective trigger has fired: the vacancy is
    # untouched -- it has no player, hence no match, for a match-scoped
    # selective trigger to ever cover.
    after_selective = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    assert after_selective.positions["F1"].state == LockState.LOCKED
    assert after_selective.positions["Interchange"].state == LockState.EDITABLE
    assert after_selective.positions["Interchange"].reason == "empty"
    assert vacancy_rows(draft.lineup_id) == []

    # 3. Main activates: the read model now distinguishes this from
    # ordinary persisted player-level lock evidence (F1's, backed by a real
    # `weekly_lineup_lock` row) -- the vacancy is reported LOCKED with the
    # main trigger itself as the authoritative reason, `season_player_id`
    # still `None`, and `irreversible` False, because there is not, and can
    # never be, a `weekly_lineup_lock` row for an empty position.
    after_main = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=LATE_START,
    )
    assert after_main.positions["F1"].state == LockState.LOCKED
    assert after_main.positions["F1"].irreversible is True
    assert after_main.positions["Interchange"].state == LockState.LOCKED
    assert after_main.positions["Interchange"].reason == "main_lockout_triggered"
    assert after_main.positions["Interchange"].season_player_id is None
    assert after_main.positions["Interchange"].afl_match_id is None
    assert after_main.positions["Interchange"].irreversible is False
    # The decisive check: no fabricated/persisted player-level row exists
    # for the vacant position, even though materialization has now run
    # against a durably-activated main trigger.
    assert vacancy_rows(draft.lineup_id) == []

    # Atomic rejection: an attempt to populate the now-immutable vacancy
    # after main is refused wholesale, alongside everything else in the
    # same submission -- never partially applied.
    late_filler = acquire(pool, ownership, scope, entry, 2, LATE_HOME, name="Post-Main Interchange Filler")
    guard_main = lock_repo.guard(match_facts=matches, evaluation_at=LATE_START)
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": early.season_player_id, "Interchange": late_filler.season_player_id},
        from_revision=draft.revision,
    )
    with pytest.raises(LockedSelectionError, match="Interchange"):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard_main,
        )
    assert lineups.get_effective_submission(draft.lineup_id).positions == submitted.positions
    assert vacancy_rows(draft.lineup_id) == []


def test_clearing_an_already_locked_position_in_an_unsubmitted_draft_still_reports_it_locked():
    """Issue #138 (Codex review, PR #143): `guard_transition` already
    refuses to ever clear an effectively-locked position -- its first loop
    rejects any change away from a locked position's previous value,
    regardless of the proposed replacement. But before this fix, a caller
    evaluating a *different* `positions` mapping where that same slot now
    reads vacant (e.g. a rejected save-then-submit attempt that cleared it
    in the private draft) saw it reported as an ordinary open vacancy --
    `_evaluate_position`'s `season_player_id is None` branch returned
    editable/"empty" unconditionally, without ever consulting the
    position's own persisted lock evidence first. A delegated/coach UI
    rendering directly from that read model would then present an
    authoritatively locked position as an editable, empty dropdown."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_lock_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_lock_guard)

    lock_repo = LockoutRepository(db)
    # Materialise F1's lock evidence (mirrors what a rejected submit
    # attempt's earlier successful GET/`lock_state` call would already
    # have done).
    lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )

    # An unsubmitted draft that attempted (and would have had rejected) to
    # clear the now-locked F1 -- this must never be evaluated as an
    # invented, ordinary vacancy.
    cleared_draft_positions = dict(submitted.positions)
    cleared_draft_positions["F1"] = None
    view = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        cleared_draft_positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=2),
    )
    assert view.positions["F1"].state == LockState.LOCKED
    assert view.positions["F1"].season_player_id == early.season_player_id
    assert view.positions["F1"].reason == "selective_trigger_activated"
    assert view.positions["F1"].irreversible is True

    # A genuinely never-selected vacancy elsewhere is completely unaffected.
    assert view.positions["M1"].state == LockState.EDITABLE
    assert view.positions["M1"].reason == "empty"

    # And the guard itself, unchanged, still refuses the actual clearing
    # attempt -- this fix only corrects the read model's *presentation*,
    # never the authoritative accept/reject decision.
    draft2 = edit_draft(
        lineups, round_, entry, scope, draft.lineup_id, cleared_draft_positions, from_revision=draft.revision
    )
    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=2))
    with pytest.raises(LockedSelectionError, match="F1"):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=late_guard,
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


class SteppingMatchFacts:
    """A `MatchFactsProvider` double whose `evaluation_at()` advances by one
    step on every call -- used to prove `LockGuard` samples "now" *once*
    per submission attempt, never once for `materialize()` and again,
    independently, for `__call__` (issue #144 Codex review, P1). An
    explicit `evaluation_at` passed to `LockoutRepository.guard` is
    unaffected either way -- `_evaluation_at` returns it verbatim without
    ever consulting this double -- so only a live, wall-clock-driven guard
    (`evaluation_at=None`, the production default) exercises this."""

    def __init__(self, matches, times):
        self.matches = list(matches)
        self._times = iter(times)
        self.calls = 0

    def matches_for(self, bbbffl_round_id):
        return self.matches

    def evaluation_at(self):
        self.calls += 1
        return next(self._times)


def test_lock_guard_samples_one_evaluation_instant_for_the_whole_submission():
    """Before this fix, a `LockGuard` built with no explicit `evaluation_at`
    (the production default) called `_at()` independently in `materialize()`
    and again in `__call__` -- so a real wall-clock advance between those
    two steps of *one* submission attempt could let them disagree about
    "now". `materialize()`'s own instant is what durably governs a
    newly-discovered lock (`_insert_lock`'s `effective_lock_at`/`locked_at`
    columns), so a `__call__` that silently computed a later, unused
    instant of its own violated this module's documented invariant that
    `guard_transition` always operates against the *same* moment
    `materialize()` just recorded evidence for. Fixed: `LockGuard._at()`
    memoizes the first-resolved instant per instance, so the underlying
    clock is consulted exactly once per submission attempt."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    uncovered = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    pre_guard = LockoutRepository(db).guard(
        match_facts=FakeMatchFacts(ALL_MATCHES), evaluation_at=EARLY_START - timedelta(days=1)
    )
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_guard)

    edit = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": early.season_player_id, "M1": uncovered.season_player_id},
        from_revision=draft.revision,
    )
    # Two distinct instants queued: if the clock were sampled twice (the
    # pre-fix bug), `materialize()` and `__call__` would silently disagree.
    stepping = SteppingMatchFacts(
        ALL_MATCHES,
        [EARLY_START - timedelta(milliseconds=1), EARLY_START + timedelta(milliseconds=1)],
    )
    live_guard = LockoutRepository(db).guard(match_facts=stepping, evaluation_at=None)
    changed = lineups.submit(
        edit.lineup_id,
        expected_draft_revision=edit.revision,
        expected_submission_version=submitted.version,
        lock_guard=live_guard,
    )
    assert changed.positions["M1"] == uncovered.season_player_id
    # The decisive assertion: the clock was consulted exactly once for this
    # whole submission attempt, not once per LockGuard method.
    assert stepping.calls == 1


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
    # issue #185: unconfirmed against `resolve_byes` (this `FakeMatchFacts`
    # implements no `byes_for` at all), so this stays the ordinary
    # unresolved message, never the confirmed-bye one.
    assert "cannot be selected because No Match Scheduled" in view.positions["F1"].reason


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


# ---------------------------------------------------------------------------
# Replay-oriented: the round lockout plan driven by curated AFL evidence
# fixtures (issue #40 / roadmap package 08), not a hand-built Match list.
# ---------------------------------------------------------------------------


def test_round_lockout_plan_activates_from_curated_afl_evidence_fixtures():
    """Proves roadmap package 08's evidence corpus is actually useful to
    #34's staged lockouts: `RoundMatchFacts` sources match facts from
    `tests/fixtures/afl_evidence/v1/synthetic/season_85/round_1500/matches.json`,
    parsed by the real `AflApiClient` (zero network -- see
    `tests/test_afl_evidence.py`), and the same
    `LockoutTriggerRepository`/`LockoutRepository` machinery used throughout
    this module reacts to it exactly as it would to live afl-api data.

    Round 1500's fixture has match 9502 (Geelong v GWS Giants) already LIVE
    -- an early/selective trigger on it activates immediately regardless of
    evaluation time -- and match 9501 (Richmond v Fremantle) still UPCOMING
    with a scheduled start, driving the round's main trigger once that time
    passes.
    """
    db, _, round_, entries, scope, pool, ownership = context(afl_round=1500)
    entry = entries[0]
    client = afl_evidence.build_client(
        {"/api/v1/rounds/1500/matches": "v1/synthetic/season_85/round_1500/matches.json"}
    )
    try:
        match_facts = afl_evidence.RoundMatchFacts(client, afl_round_id=1500)
        triggers = LockoutTriggerRepository(db)
        configure_selective(triggers, round_.bbbffl_round_id, [9502], key="early-1", sequence=1)
        configure_main(triggers, round_.bbbffl_round_id, [9501], sequence=2)
        early = acquire(pool, ownership, scope, entry, 900001, Team(6003, "Geelong"))
        other = acquire(pool, ownership, scope, entry, 900002, Team(6001, "Richmond"))
        lineups = WeeklyLineupRepository(db)
        draft, submitted = establish(
            lineups, round_, entry, scope, {"F1": early.season_player_id, "M1": other.season_player_id}
        )
        lock_repo = LockoutRepository(db)

        before_main_start = lock_repo.lock_state(
            draft.lineup_id,
            round_.bbbffl_round_id,
            entry.season_entry_id,
            submitted.positions,
            match_facts=match_facts,
            evaluation_at=datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc),
        )
        assert before_main_start.positions["F1"].state == LockState.LOCKED
        assert before_main_start.positions["F1"].reason == "selective_trigger_activated"
        assert before_main_start.positions["M1"].state == LockState.EDITABLE

        after_main_start = lock_repo.lock_state(
            draft.lineup_id,
            round_.bbbffl_round_id,
            entry.season_entry_id,
            submitted.positions,
            match_facts=match_facts,
            evaluation_at=datetime(2026, 7, 5, 9, 0, tzinfo=timezone.utc),
        )
        assert after_main_start.positions["F1"].state == LockState.LOCKED  # unchanged, still via the early trigger
        assert after_main_start.positions["M1"].state == LockState.LOCKED
        assert after_main_start.positions["M1"].reason == "main_lockout_triggered"
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Issue #176: persisted-only historical projection -- never asks a
# MatchFactsProvider for anything, so a round outside the currently active
# replay evidence package can still have its already-decided lockout
# history redisplayed safely.
# ---------------------------------------------------------------------------


def test_persisted_lock_state_projects_durable_evidence_without_match_facts():
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    early = acquire(pool, ownership, scope, entry, 900001, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id, "M1": None})
    lock_repo = LockoutRepository(db)

    # Materialize durable evidence exactly as the live/replay flow would --
    # both matches concluded, so the selective trigger locks F1 by its own
    # persisted `weekly_lineup_lock` row and the main trigger's activation
    # (a pure persisted fact) locks the deliberately vacant M1.
    match_facts = FakeMatchFacts([early_match(status="CONCLUDED"), late_match(status="CONCLUDED")])
    lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=match_facts,
        evaluation_at=LATE_START + timedelta(hours=1),
    )

    projected = lock_repo.persisted_lock_state(
        draft.lineup_id, round_.bbbffl_round_id, entry.season_entry_id, submitted.positions
    )

    assert projected.positions["F1"].state == LockState.LOCKED
    assert projected.positions["F1"].reason == "selective_trigger_activated"
    assert projected.positions["M1"].state == LockState.LOCKED
    assert projected.positions["M1"].reason == "main_lockout_triggered"


def test_persisted_lock_state_honors_main_activation_for_a_never_materialized_named_position():
    """Codex review (PR #177): a persisted main-trigger activation
    conclusively locks *every remaining position* once it has fired --
    including a named occupant whose own `weekly_lineup_lock` row was never
    separately materialized. Lock rows are written lazily, per lineup
    (`_materialize_lineup`), and nothing guarantees every lineup was
    re-observed again after main lockout actually activated -- so this must
    never be reported `INDETERMINATE` merely because that lazy write never
    happened for this one lineup."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=1)
    named = acquire(pool, ownership, scope, entry, 900003, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": named.season_player_id})
    lock_repo = LockoutRepository(db)

    # Durably activate the main trigger -- but deliberately never call
    # `lock_state`/`materialize_lineup` for this lineup, so no
    # `weekly_lineup_lock` row is ever written for F1.
    lock_repo.materialize_round_triggers(
        round_.bbbffl_round_id,
        match_facts=FakeMatchFacts([late_match(status="CONCLUDED")]),
        evaluation_at=LATE_START + timedelta(hours=1),
    )

    projected = lock_repo.persisted_lock_state(
        draft.lineup_id, round_.bbbffl_round_id, entry.season_entry_id, submitted.positions
    )

    assert projected.positions["F1"].state == LockState.LOCKED
    assert projected.positions["F1"].reason == "main_lockout_triggered"
    assert projected.positions["F1"].irreversible is False


def test_persisted_lock_state_reports_indeterminate_for_a_never_materialized_position():
    """A named position that never durably locked (its match was never
    supplied to any prior `lock_state`/materialization call) must be
    reported `INDETERMINATE`, never guessed live -- this method has no live
    evidence to guess from in the first place."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    never_locked = acquire(pool, ownership, scope, entry, 900002, LATE_HOME)
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": never_locked.season_player_id})

    projected = LockoutRepository(db).persisted_lock_state(
        draft.lineup_id, round_.bbbffl_round_id, entry.season_entry_id, submitted.positions
    )

    assert projected.positions["F1"].state == LockState.INDETERMINATE
    assert projected.positions["F1"].reason == "evidence_unavailable_for_historical_round"


def test_persisted_trigger_state_projects_activation_without_match_facts():
    db, _, round_, entries, scope, pool, ownership = context()
    triggers = LockoutTriggerRepository(db)
    # The selective trigger is scoped to the *later*-scheduled match, so it
    # stays un-activated at an evaluation instant that has already locked
    # the main trigger's own (earlier, concluded) match.
    configure_main(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], sequence=1)
    configure_selective(triggers, round_.bbbffl_round_id, [STAGE_B_MATCH_ID], key="stage-b", sequence=2)
    lock_repo = LockoutRepository(db)

    lock_repo.materialize_round_triggers(
        round_.bbbffl_round_id,
        match_facts=FakeMatchFacts([early_match(status="CONCLUDED"), stage_b_match(status="UPCOMING")]),
        evaluation_at=EARLY_START + timedelta(hours=1),
    )

    views = {view.trigger_key: view for view in lock_repo.persisted_trigger_state(round_.bbbffl_round_id)}

    assert views["stage-b"].activated is False
    assert views["main"].activated is True
    assert views["main"].activation_reason == "match_status_completed"
    # No live evidence was ever asked for by this projection, so per-match
    # observed status/start time is unavailable -- never fabricated.
    for view in views.values():
        for configured_match in view.configured_matches:
            assert configured_match["observed_status"] is None
            assert configured_match["start_time_utc"] is None


# ---------------------------------------------------------------------------
# Issue #185: invalid selection (known AFL bye) vs. actual position lock.
# ---------------------------------------------------------------------------


def test_bye_player_selection_is_invalid_but_position_stays_editable_before_lockout():
    """A player whose AFL club has no match this round is an invalid
    selection, but -- unlike the old, over-broad INDETERMINATE handling
    the 2026 Round 12 replay exposed -- the position itself is not locked:
    no selective trigger can ever cover a club with no match to resolve to,
    and main has not activated here."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope, entry, 1, BYE_TEAM, name="Bye Club Player")
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES, byes=frozenset({BYE_TEAM.team_id}))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": bye_player.season_player_id})

    view = LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )
    f1 = view.positions["F1"]
    assert f1.state == LockState.INVALID_SELECTION
    assert f1.season_player_id == bye_player.season_player_id
    assert f1.reason == "This player cannot be selected because Bye FC have no AFL match in this round."
    assert f1.irreversible is False
    assert f1.afl_match_id is None


def test_ordinary_submission_rejects_an_unchanged_bye_player_still_selected():
    """A position that already holds a bye player (e.g. carried over from an
    earlier submission) must keep failing ordinary submission even when
    that position is not itself being edited -- the selection, not just the
    change, is what is invalid."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope, entry, 1, BYE_TEAM)
    other = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES, byes=frozenset({BYE_TEAM.team_id}))
    draft, submitted = establish(
        lineups, round_, entry, scope, {"F1": bye_player.season_player_id, "M1": other.season_player_id}
    )

    # Resubmit with F1 left completely unchanged -- only M2 differs.
    ruck = acquire(pool, ownership, scope, entry, 3, UNCOVERED_HOME, name="Ruck Pick")
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {**submitted.positions, "Ruck": ruck.season_player_id},
        from_revision=draft.revision,
    )
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    with pytest.raises(InvalidSelectionError, match="Bye FC"):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard,
        )


def test_ordinary_submission_rejects_a_newly_proposed_bye_player():
    """Introducing a bye player for the first time is refused identically
    to leaving one unchanged -- `InvalidSelectionError`, never silently
    accepted just because the position itself was open."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope, entry, 1, BYE_TEAM)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES, byes=frozenset({BYE_TEAM.team_id}))
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": bye_player.season_player_id},
        expected_revision=0,
    )
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    with pytest.raises(InvalidSelectionError, match="Bye FC"):
        lineups.submit(
            draft.lineup_id,
            expected_draft_revision=draft.revision,
            expected_submission_version=0,
            lock_guard=guard,
        )


def test_replacing_a_bye_player_before_lockout_lets_normal_submission_succeed():
    """The core acceptance criterion: once the invalid player is replaced
    with an otherwise eligible squad member, ordinary submission succeeds
    exactly as normal."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope, entry, 1, BYE_TEAM)
    replacement = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME, name="Valid Replacement")
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES, byes=frozenset({BYE_TEAM.team_id}))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": bye_player.season_player_id})

    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {**submitted.positions, "F1": replacement.season_player_id},
        from_revision=draft.revision,
    )
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    resubmitted = lineups.submit(
        draft2.lineup_id,
        expected_draft_revision=draft2.revision,
        expected_submission_version=submitted.version,
        lock_guard=guard,
    )
    assert resubmitted.positions["F1"] == replacement.season_player_id

    # And the read model now reports it as an ordinary editable position,
    # not an invalid selection.
    view = LockoutRepository(db).lock_state(
        draft2.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        resubmitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START - timedelta(minutes=1),
    )
    assert view.positions["F1"].state == LockState.EDITABLE


def test_selective_lockout_never_covers_a_bye_position_it_stays_an_invalid_selection():
    """A bye player can never be covered by a *selective* trigger -- those
    are keyed by specific AFL match ids, and a bye player resolves to none.
    Activating a selective trigger elsewhere in the round must not turn the
    bye position into a genuinely locked one, and a genuinely selectively-
    locked position elsewhere remains immutable exactly as before."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    bye_player = acquire(pool, ownership, scope, entry, 2, BYE_TEAM)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES, byes=frozenset({BYE_TEAM.team_id}))
    draft, submitted = establish(
        lineups, round_, entry, scope, {"F1": early.season_player_id, "M1": bye_player.season_player_id}
    )

    view = LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )
    assert view.positions["F1"].state == LockState.LOCKED
    assert view.positions["F1"].reason == "selective_trigger_activated"
    assert view.positions["M1"].state == LockState.INVALID_SELECTION

    # The guard still refuses to touch the genuinely locked F1 -- even while
    # M1's unrelated invalid selection is simultaneously being replaced, so
    # this failure is decisively about F1's lock, not M1's still-pending bye.
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    other = acquire(pool, ownership, scope, entry, 3, EARLY_HOME, name="Blocked F1 Replacement")
    replacement = acquire(pool, ownership, scope, entry, 4, UNCOVERED_HOME, name="M1 Replacement")
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {**submitted.positions, "F1": other.season_player_id, "M1": replacement.season_player_id},
        from_revision=draft.revision,
    )
    with pytest.raises(LockedSelectionError):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard,
        )
    # ...while M1's bye player, considered alone (F1 left genuinely
    # unchanged this time), can still be freely replaced.
    draft3 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {**submitted.positions, "M1": replacement.season_player_id},
        from_revision=draft2.revision,
    )
    resubmitted = lineups.submit(
        draft3.lineup_id,
        expected_draft_revision=draft3.revision,
        expected_submission_version=submitted.version,
        lock_guard=guard,
    )
    assert resubmitted.positions["M1"] == replacement.season_player_id


def test_main_lockout_freezes_a_bye_position_exactly_like_any_other_remaining_position():
    """Once the round's main trigger activates, an invalid selection
    collapses into an ordinary locked position -- main freezes every
    remaining position regardless of the player, and an invalid selection
    is exactly such a remaining position."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID])
    bye_player = acquire(pool, ownership, scope, entry, 1, BYE_TEAM)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES, byes=frozenset({BYE_TEAM.team_id}))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": bye_player.season_player_id})
    lock_repo = LockoutRepository(db)

    before = lock_repo.lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=LATE_START - timedelta(minutes=1),
    )
    assert before.positions["F1"].state == LockState.INVALID_SELECTION

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
    assert after.positions["F1"].irreversible is False  # see 'Deliberately vacant positions': nothing to persist

    # Ordinary editing is now refused exactly like any other locked
    # position -- lockout immutability is unchanged by this fix.
    replacement = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME, name="Too Late Replacement")
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {**submitted.positions, "F1": replacement.season_player_id},
        from_revision=draft.revision,
    )
    late_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=LATE_START)
    with pytest.raises(LockedSelectionError):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=late_guard,
        )


def test_unresolved_evidence_is_never_misclassified_as_a_safe_editable_bye_correction():
    """A genuinely unresolved case (no cached AFL club at all) must keep
    failing closed exactly as before -- it must never be downgraded to the
    new, editable INVALID_SELECTION state merely because some evidence
    happens to be absent. Fail-closed behaviour for genuinely unresolved
    evidence is unchanged by this fix."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    unresolved = pool.refresh_player(scope["season_id"], 887766, "Unknown Club Player")
    ownership.acquire(unresolved.season_player_id, entry.season_entry_id)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": unresolved.season_player_id})

    view = LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )
    assert view.positions["F1"].state == LockState.INDETERMINATE
    assert "no known AFL club" in view.positions["F1"].reason

    # Fail-closed: a differing replacement is refused exactly like any other
    # indeterminate position -- never treated as freely editable.
    replacement = acquire(pool, ownership, scope, entry, 3, UNCOVERED_HOME, name="Blocked Replacement")
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {**submitted.positions, "F1": replacement.season_player_id},
        from_revision=draft.revision,
    )
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    with pytest.raises(LockedSelectionError, match="indeterminate"):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard,
        )


def test_round_match_facts_provider_byes_for_resolves_confirmed_byes():
    """`RoundMatchFactsProvider.byes_for` -- the production implementation
    `resolve_byes` calls -- reuses the accepted round mapping to fetch
    afl-api's own round-bye metadata, exactly like `app.lineup_validation`'s
    availability advisory already does, never a duplicated hard-coded team
    table (issue #185)."""
    db, _, round_, entries, scope, pool, ownership = context()
    provider = RoundMatchFactsProvider(
        RoundMappingRepository(db),
        SimpleNamespace(
            get_matches=lambda afl_round_id: ALL_MATCHES,
            get_rounds=lambda afl_season_id: [SimpleNamespace(round_id=2027, round_number=1, byes=(BYE_TEAM,))],
        ),
    )
    assert resolve_byes(provider, round_.bbbffl_round_id) == frozenset({BYE_TEAM.team_id})


def test_round_match_facts_provider_byes_for_returns_none_when_unresolved():
    """`byes_for`/`resolve_byes` return `None` -- never a guessed empty
    set -- whenever byes cannot be positively established: no matching
    round in afl-api's response, or afl-api reporting `byes=None` for the
    round. Either way, `_evaluate_position` must keep failing closed rather
    than infer a confirmation that was never actually given."""
    db, _, round_, entries, scope, pool, ownership = context()
    no_matching_round = RoundMatchFactsProvider(
        RoundMappingRepository(db),
        SimpleNamespace(get_matches=lambda afl_round_id: ALL_MATCHES, get_rounds=lambda afl_season_id: []),
    )
    assert resolve_byes(no_matching_round, round_.bbbffl_round_id) is None

    byes_not_reported = RoundMatchFactsProvider(
        RoundMappingRepository(db),
        SimpleNamespace(
            get_matches=lambda afl_round_id: ALL_MATCHES,
            get_rounds=lambda afl_season_id: [SimpleNamespace(round_id=2027, round_number=1, byes=None)],
        ),
    )
    assert resolve_byes(byes_not_reported, round_.bbbffl_round_id) is None

    # A provider with no `byes_for` at all (every plain `FakeMatchFacts`
    # constructed without `byes=`, and any caller written before issue
    # #185) is unconfirmed, not an error.
    assert resolve_byes(FakeMatchFacts(ALL_MATCHES), round_.bbbffl_round_id) is None


def test_bye_player_fails_closed_when_no_lockout_plan_is_configured():
    """issue #185 Codex re-review: a confirmed bye must still fail closed
    exactly like an ordinary resolvable player when the round has no
    lockout plan configured at all -- this module has no basis for knowing
    whether some future trigger configuration would have covered the
    position, so it must never report a safely-editable INVALID_SELECTION
    in that scenario. Before this fix, the bye branch checked only
    `coverage.main_activated`, skipping the `lockout_plan_not_configured`
    fail-closed check every other position already respects."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    # Deliberately no LockoutTriggerRepository configuration at all.
    bye_player = acquire(pool, ownership, scope, entry, 1, BYE_TEAM)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES, byes=frozenset({BYE_TEAM.team_id}))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": bye_player.season_player_id})

    view = LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START - timedelta(minutes=5),
    )
    assert view.positions["F1"].state == LockState.INDETERMINATE
    assert view.positions["F1"].reason == "lockout_plan_not_configured"

    # And ordinary submission fails closed exactly like any other
    # indeterminate position -- never treated as a freely editable/
    # clearable invalid selection just because no plan exists yet.
    replacement = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME, name="Blocked Replacement")
    draft2 = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {**submitted.positions, "F1": replacement.season_player_id},
        from_revision=draft.revision,
    )
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    with pytest.raises(LockedSelectionError, match="indeterminate"):
        lineups.submit(
            draft2.lineup_id,
            expected_draft_revision=draft2.revision,
            expected_submission_version=submitted.version,
            lock_guard=guard,
        )
