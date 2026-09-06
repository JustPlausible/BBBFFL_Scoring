from types import SimpleNamespace

from app.coach_lineup import CoachLineupService
from app.lineups import POSITIONS, WeeklyLineupRepository
from app.lockouts import LockoutRepository, LockoutTriggerRepository, LockState
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import operational
from tests.test_lockouts import (
    ALL_MATCHES,
    LATE_HOME,
    LATE_MATCH_ID,
    LATE_START,
    FakeMatchFacts,
    acquire,
    configure_main,
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
