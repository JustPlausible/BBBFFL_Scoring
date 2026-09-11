from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.coach_lineup import CoachLineupService, describe_ordinary_position
from app.lineups import POSITIONS, WeeklyLineupRepository
from app.lockouts import InvalidSelectionError, LockoutRepository, LockoutTriggerRepository, LockState
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import operational
from tests.test_lockouts import (
    ALL_MATCHES,
    BYE_TEAM,
    EARLY_HOME,
    EARLY_MATCH_ID,
    EARLY_START,
    LATE_HOME,
    LATE_MATCH_ID,
    LATE_START,
    UNCOVERED_HOME,
    FakeMatchFacts,
    acquire,
    configure_main,
    configure_selective,
    context,
    establish,
)


def test_reopening_draft_uses_current_owned_squad_without_rewriting_selection(monkeypatch):
    database = migrated_connection()
    _, round_, entries = operational(database, 2031, 301)
    entry = entries[0]
    coach = database.execute(
        "SELECT coach_id FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
        (entry.season_entry_id,),
    ).fetchone()
    scope = database.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()

    pool = PlayerPoolRepository(database)
    ownership = OwnershipRepository(database)
    ownership.configure_squad_limit(scope["season_id"], 9)
    released = pool.refresh_player(scope["season_id"], 91001, "Released Player")
    acquired = pool.refresh_player(scope["season_id"], 91002, "Newly Acquired Player")
    ownership.acquire(released.season_player_id, entry.season_entry_id, effective_at="2031-01-01T00:00:00+00:00")

    service = CoachLineupService(database, afl_client=SimpleNamespace())
    monkeypatch.setattr(service.lockouts, "lock_state", lambda *args, **kwargs: SimpleNamespace(positions={}))
    entry_context = service.resolve(coach["coach_id"], scope["season_id"], round_.bbbffl_round_id)
    draft = service.ensure_draft(scope["season_id"], round_.bbbffl_round_id, entry_context)
    positions = dict.fromkeys(POSITIONS)
    positions["F1"] = released.season_player_id
    saved = service.save(scope["season_id"], round_.bbbffl_round_id, entry_context, positions, draft.revision)

    ownership.release(released.season_player_id, effective_at="2031-02-01T00:00:00+00:00")
    ownership.acquire(acquired.season_player_id, entry.season_entry_id, effective_at="2031-02-01T00:00:01+00:00")

    reopened = service.view(coach["coach_id"], scope["season_id"], round_.bbbffl_round_id)
    offered = {player.season_player_id for player in reopened.players}

    assert acquired.season_player_id in offered
    assert released.season_player_id not in offered
    assert reopened.draft.positions["F1"] == released.season_player_id
    assert reopened.selected_players["F1"].season_player_id == released.season_player_id
    assert reopened.draft.revision == saved.revision
    persisted = service.lineups.get_draft(
        scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id
    )
    assert persisted.positions["F1"] == released.season_player_id
    assert persisted.revision == saved.revision


def test_view_reports_a_main_locked_vacancy_from_the_submission_not_a_rejected_draft_fill():
    """Issue #155 (Codex review, PR #157): `lineup_action`'s save-then-submit
    convention means a coach's rejected attempt to populate a vacancy Main
    has already locked is persisted to the private draft *before* `submit`
    rejects it and the page re-renders. `view()` must evaluate immutability
    against the lineup's effective submission (still vacant), never echo
    the rejected draft value back as though it were the authoritative
    locked selection -- exactly the class of bug issue #138 already fixed
    for the delegated surface."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    coach_row = db.execute(
        "SELECT coach_id FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
        (entry.season_entry_id,),
    ).fetchone()
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID])
    rejected_pick = acquire(pool, ownership, scope, entry, 1, LATE_HOME, name="Rejected Post-Main Pick")
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    draft, submitted = establish(lineups, round_, entry, scope, {})
    assert submitted.positions["Interchange"] is None

    # Materialize Main's activation -- mirrors an earlier GET/submit attempt
    # having already observed it, exactly as production always does before
    # any rejection can occur.
    LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=LATE_START,
    )

    service = CoachLineupService(
        db,
        afl_client=SimpleNamespace(get_matches=lambda afl_round_id: ALL_MATCHES, get_rounds=lambda afl_season_id: []),
    )
    entry_context = service.resolve(coach_row["coach_id"], scope["season_id"], round_.bbbffl_round_id)
    # Mirrors `app.routes.coach_lineup.lineup_action`'s save-then-submit:
    # the rejected fill attempt is already persisted to the draft by the
    # time a caught `LockedSelectionError` re-renders the page.
    service.save(
        scope["season_id"],
        round_.bbbffl_round_id,
        entry_context,
        {**submitted.positions, "Interchange": rejected_pick.season_player_id},
        draft.revision,
    )

    rendered = service.view(coach_row["coach_id"], scope["season_id"], round_.bbbffl_round_id)
    assert rendered.draft.positions["Interchange"] == rejected_pick.season_player_id

    interchange_lock = rendered.locks["Interchange"]
    assert interchange_lock.state == LockState.LOCKED
    assert interchange_lock.reason == "main_lockout_triggered"
    # The decisive assertions: the authoritative (still-vacant) value, never
    # the coach's own rejected pick.
    assert interchange_lock.season_player_id is None
    assert rendered.selected_players["Interchange"] is None


# ---------------------------------------------------------------------------
# Issue #185: coach flow presentation of an invalid (bye-player) selection.
# ---------------------------------------------------------------------------


def test_coach_view_reports_a_bye_player_as_editable_with_a_replacement_offered():
    """Before any lockout, a bye player is reported editable, not disabled
    like a genuinely locked/indeterminate position -- and the coach's own
    current owned squad (including an eligible replacement) is still
    offered for selection, exactly like any other editable position."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    coach_row = db.execute(
        "SELECT coach_id FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
        (entry.season_entry_id,),
    ).fetchone()
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope, entry, 1, BYE_TEAM, name="Bye Club Player")
    replacement = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME, name="Valid Replacement")
    lineups = WeeklyLineupRepository(db)
    establish(lineups, round_, entry, scope, {"F1": bye_player.season_player_id})

    # `context()`'s default `operational(db, 2027, None)` accepts a mapping
    # of afl_season_id=2027, afl_round_id=2027 (see test_competition_
    # lifecycle.configured) -- the fake `get_rounds` below must answer for
    # that exact afl_round_id so `RoundMatchFactsProvider.byes_for` (the
    # real production wiring, not a hand-rolled FakeMatchFacts) resolves a
    # positive bye confirmation end to end.
    service = CoachLineupService(
        db,
        afl_client=SimpleNamespace(
            get_matches=lambda afl_round_id: ALL_MATCHES,
            get_rounds=lambda afl_season_id: [SimpleNamespace(round_id=2027, round_number=1, byes=(BYE_TEAM,))],
        ),
    )
    rendered = service.view(coach_row["coach_id"], scope["season_id"], round_.bbbffl_round_id)

    f1 = rendered.locks["F1"]
    assert f1.state == LockState.INVALID_SELECTION
    assert "Bye FC" in f1.reason
    # The coach can still see/select the eligible replacement.
    offered = {player.season_player_id for player in rendered.players}
    assert replacement.season_player_id in offered
    assert rendered.selected_players["F1"].season_player_id == bye_player.season_player_id


def test_coach_view_reflects_a_draft_replacement_saved_over_a_submitted_bye_player():
    """Codex review (PR #186): once the coach saves a replacement to their
    private draft for a position the effective submission holds a bye
    player in, `view()` must render the replacement, not keep echoing the
    old submitted bye player -- the same "still-open position defers to the
    live draft" rule `EDITABLE` already got, now extended to
    `INVALID_SELECTION` positions too. Before this fix, the overlay only
    fired for `EDITABLE`, so a saved replacement for a bye position never
    actually rendered, and a subsequent Submit would resend the stale bye
    player and be rejected again."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    coach_row = db.execute(
        "SELECT coach_id FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
        (entry.season_entry_id,),
    ).fetchone()
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope, entry, 1, BYE_TEAM, name="Bye Club Player")
    replacement = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME, name="Valid Replacement")
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": bye_player.season_player_id})

    service = CoachLineupService(
        db,
        afl_client=SimpleNamespace(
            get_matches=lambda afl_round_id: ALL_MATCHES,
            get_rounds=lambda afl_season_id: [SimpleNamespace(round_id=2027, round_number=1, byes=(BYE_TEAM,))],
        ),
    )
    entry_context = service.resolve(coach_row["coach_id"], scope["season_id"], round_.bbbffl_round_id)
    service.save(
        scope["season_id"],
        round_.bbbffl_round_id,
        entry_context,
        {**submitted.positions, "F1": replacement.season_player_id},
        draft.revision,
    )

    rendered = service.view(coach_row["coach_id"], scope["season_id"], round_.bbbffl_round_id)
    assert rendered.locks["F1"].state == LockState.EDITABLE
    assert rendered.locks["F1"].season_player_id == replacement.season_player_id
    assert rendered.selected_players["F1"].season_player_id == replacement.season_player_id


def test_coach_view_keeps_an_invalid_selection_editable_after_a_bad_draft_candidate():
    """Codex re-review (PR #186): saving a draft replacement that would
    itself currently be rejected (e.g. a player whose own match an
    already-activated selective trigger now covers) must never make an
    INVALID_SELECTION position render as locked/indeterminate. Before this
    fix, the draft-overlay blindly trusted the draft candidate's own
    evaluation, so one bad saved pick left the coach with no further way to
    correct the position through the UI -- even though the true
    authoritative position (the still-selected bye player) remains
    genuinely open until main lockout activates."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    coach_row = db.execute(
        "SELECT coach_id FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
        (entry.season_entry_id,),
    ).fetchone()
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope, entry, 1, BYE_TEAM, name="Bye Club Player")
    bad_candidate = acquire(pool, ownership, scope, entry, 2, EARLY_HOME, name="Now-Locked Candidate")
    lineups = WeeklyLineupRepository(db)
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": bye_player.season_player_id})

    # Durably activate the selective trigger before the coach saves their
    # (bad) draft pick -- mirrors an earlier GET having already observed
    # it, exactly as production always does before any save can occur.
    matches = FakeMatchFacts(ALL_MATCHES, byes=frozenset({BYE_TEAM.team_id}))
    LockoutRepository(db).lock_state(
        draft.lineup_id,
        round_.bbbffl_round_id,
        entry.season_entry_id,
        submitted.positions,
        match_facts=matches,
        evaluation_at=EARLY_START + timedelta(minutes=1),
    )

    service = CoachLineupService(
        db,
        afl_client=SimpleNamespace(
            get_matches=lambda afl_round_id: ALL_MATCHES,
            get_rounds=lambda afl_season_id: [SimpleNamespace(round_id=2027, round_number=1, byes=(BYE_TEAM,))],
        ),
    )
    entry_context = service.resolve(coach_row["coach_id"], scope["season_id"], round_.bbbffl_round_id)
    service.save(
        scope["season_id"],
        round_.bbbffl_round_id,
        entry_context,
        {**submitted.positions, "F1": bad_candidate.season_player_id},
        draft.revision,
    )

    rendered = service.view(coach_row["coach_id"], scope["season_id"], round_.bbbffl_round_id)
    # Still an editable invalid selection -- the original submitted bye
    # player, never the now-locked bad candidate, and never disabled.
    assert rendered.locks["F1"].state == LockState.INVALID_SELECTION
    assert rendered.locks["F1"].season_player_id == bye_player.season_player_id


def test_describe_ordinary_position_renders_invalid_selection_as_an_editable_control():
    """Direct unit coverage of the shared presentation boundary both the
    Coach and delegated Replay Operator surfaces call (issue #138/#185):
    `editable` is `True` (never disabled like locked/indeterminate) and the
    already-human-readable reason is surfaced verbatim, not mangled by
    `humanize_lock_reason`'s `.capitalize()` (which would lower-case the
    club name)."""
    from app.lockouts import PositionLockState

    lock = PositionLockState(
        "F1",
        "player-1",
        LockState.INVALID_SELECTION,
        "This player cannot be selected because Adelaide Crows have no AFL match in this round.",
        None,
        None,
        None,
        False,
    )
    row = describe_ordinary_position("F1", lock, None, None, "player-1", None)
    assert row["state"] == "invalid_selection"
    assert row["editable"] is True
    assert row["lock_type"] == "invalid_selection"
    assert row["reason_code"] == lock.reason
    assert (
        row["reason_display"]
        == "This player cannot be selected because Adelaide Crows have no AFL match in this round."
    )
    assert row["draft_diverges"] is False


def test_coach_submission_is_rejected_while_bye_player_remains_selected():
    """The ordinary coach submission path (`WeeklyLineupRepository.submit`
    via `OpeningRoundSelectionGuard`/`LockGuard`) fails closed while an
    invalid (bye) selection remains, with the human-readable club name in
    the error the coach page shows."""
    db, _, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    bye_player = acquire(pool, ownership, scope, entry, 1, BYE_TEAM)
    lineups = WeeklyLineupRepository(db)
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": bye_player.season_player_id},
        expected_revision=0,
    )
    matches = FakeMatchFacts(ALL_MATCHES, byes=frozenset({BYE_TEAM.team_id}))
    guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(minutes=5))
    with pytest.raises(InvalidSelectionError, match="Bye FC"):
        lineups.submit(
            draft.lineup_id,
            expected_draft_revision=draft.revision,
            expected_submission_version=0,
            lock_guard=guard,
        )
