"""Pause/resume, correction/undo and finalisation for the scorer-operated
draft workflow (roadmap package 14, issue #53) -- extends the roadmap-13
ledger covered by tests/test_draft.py rather than duplicating it."""

import pytest

from app.audit import AuditEventRepository
from app.db import connect
from app.draft import (
    DraftCorrectionError,
    DraftFinalizedError,
    DraftNotCompleteError,
    DraftPausedError,
    DraftRepository,
    DraftStateError,
)
from app.identity import IdentityRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


def domain(entries=2, limit=2, players=8):
    db = migrated_connection()
    season = SeasonRepository(db).create_season(2027, "2027")
    identities = IdentityRepository(db)
    season_entries = [
        identities.create_entry(
            season.season_id, f"licence-{number}", identities.create_coach(f"Coach {number}").coach_id, f"Team {number}"
        )
        for number in range(entries)
    ]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(season.season_id, limit)
    pool = PlayerPoolRepository(db)
    season_players = [
        pool.refresh_player(season.season_id, number + 1, f"Player {number}") for number in range(players)
    ]
    draft = DraftRepository(db)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in season_entries])
    return db, season, season_entries, season_players, ownership, pool, draft


# -- Pause/resume ----------------------------------------------------------


def test_pause_blocks_pick_execution_and_resume_restores_it():
    _db, season, entries, players, _ownership, _pool, draft = domain()
    draft.pause(season.season_id, reason="dinner break")
    status = draft.status(season.season_id)
    assert status.is_paused and status.paused_reason == "dinner break"

    with pytest.raises(DraftPausedError):
        draft.execute_pick(season.season_id, entries[0].season_entry_id, players[0].season_player_id)

    draft.resume(season.season_id)
    assert draft.status(season.season_id).is_paused is False
    completed = draft.execute_pick(season.season_id, entries[0].season_entry_id, players[0].season_player_id)
    assert completed.selected_season_player_id == players[0].season_player_id


def test_pausing_twice_or_resuming_when_not_paused_is_rejected():
    _db, season, _entries, _players, _ownership, _pool, draft = domain()
    draft.pause(season.season_id)
    with pytest.raises(DraftPausedError):
        draft.pause(season.season_id)
    draft.resume(season.season_id)
    with pytest.raises(DraftStateError):
        draft.resume(season.season_id)


def test_pause_persists_across_a_fresh_connection_and_resumes_the_same_turn(tmp_path):
    url = f"sqlite:///{tmp_path / 'pause.db'}"
    from app.migrations import migrate

    migrate(url)
    db = connect(url)
    season = SeasonRepository(db).create_season(2027, "2027")
    identities = IdentityRepository(db)
    entries = [
        identities.create_entry(
            season.season_id, f"l{number}", identities.create_coach(f"C{number}").coach_id, f"T{number}"
        )
        for number in range(2)
    ]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(season.season_id, 2)
    pool = PlayerPoolRepository(db)
    players = [pool.refresh_player(season.season_id, number + 1, f"P{number}") for number in range(4)]
    draft = DraftRepository(db)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    draft.execute_pick(season.season_id, entries[0].season_entry_id, players[0].season_player_id)
    turn_before_pause = draft.next_pick(season.season_id)
    draft.pause(season.season_id, reason="overnight")
    db.close()

    # Simulate an application/process restart: a brand new connection and
    # repository instance, reading nothing but persisted database state.
    reloaded = connect(url)
    reloaded_draft = DraftRepository(reloaded)
    status = reloaded_draft.status(season.season_id)
    assert status.is_paused and status.paused_reason == "overnight"
    assert reloaded_draft.next_pick(season.season_id) == turn_before_pause

    reloaded_draft.resume(season.season_id)
    assert reloaded_draft.status(season.season_id).is_paused is False
    resumed_pick = reloaded_draft.execute_pick(
        season.season_id, turn_before_pause.current_season_entry_id, players[1].season_player_id
    )
    assert resumed_pick.draft_pick_id == turn_before_pause.draft_pick_id


# -- Correction/undo --------------------------------------------------------


def test_correction_releases_ownership_reopens_the_slot_and_keeps_history():
    _db, season, entries, players, ownership, pool, draft = domain(entries=2, limit=2)
    p1 = draft.execute_pick(
        season.season_id, entries[0].season_entry_id, players[0].season_player_id, completed_at="2027-01-01"
    )
    p2 = draft.execute_pick(
        season.season_id, entries[1].season_entry_id, players[1].season_player_id, completed_at="2027-01-02"
    )

    reopened = draft.correct_pick(
        season.season_id, p2.draft_pick_id, reason="wrong player entered", corrected_at="2027-01-02T06:00:00"
    )

    assert reopened.selected_season_player_id is None and reopened.completed_at is None
    assert reopened.overall_number == p2.overall_number
    assert reopened.current_season_entry_id == p2.current_season_entry_id
    assert draft.next_pick(season.season_id) == reopened

    # The original, erroneous row is untouched -- still shows exactly what
    # was originally selected -- and is now superseded rather than deleted.
    original_row = next(
        pick
        for pick in draft.picks(season.season_id, include_superseded=True)
        if pick.draft_pick_id == p2.draft_pick_id
    )
    assert original_row.selected_season_player_id == players[1].season_player_id
    assert original_row.completed_at == "2027-01-02"
    assert original_row.superseded_by_draft_pick_id == reopened.draft_pick_id

    # The erroneous acquisition is released (not deleted) -- squad capacity
    # and player availability are consistent again.
    history = ownership.history(players[1].season_player_id)
    assert len(history) == 1
    assert history[0].released_at == "2027-01-02T06:00:00"
    assert players[1].display_name in [player.display_name for player in pool.search_available(season.season_id)]

    # picks() (the board's normal view) shows exactly one row per slot.
    board_picks = draft.picks(season.season_id)
    assert len(board_picks) == len([p for p in board_picks])
    assert sum(1 for pick in board_picks if pick.overall_number == p2.overall_number) == 1

    # Correction history identifies original, replacement, when and why.
    [correction] = draft.corrections(season.season_id)
    assert correction.original_draft_pick_id == p2.draft_pick_id
    assert correction.replacement_draft_pick_id == reopened.draft_pick_id
    assert correction.reason == "wrong player entered"
    assert correction.corrected_at == "2027-01-02T06:00:00"

    events = AuditEventRepository(_db).list_events(action="draft.pick.corrected")
    assert len(events) == 1 and events[0].entity_id == p2.draft_pick_id

    # Re-selection goes through the ordinary, fully-validated pick path.
    corrected_selection = draft.execute_pick(
        season.season_id, entries[1].season_entry_id, players[2].season_player_id, completed_at="2027-01-02T07:00:00"
    )
    assert corrected_selection.draft_pick_id == reopened.draft_pick_id
    assert (
        ownership.owner_at(players[2].season_player_id, "2027-01-02T08:00:00").season_entry_id
        == entries[1].season_entry_id
    )
    assert p1 == draft.picks(season.season_id)[0]


def test_correction_is_narrowly_scoped_to_the_most_recent_completed_pick():
    _db, season, entries, players, _ownership, _pool, draft = domain(entries=2, limit=2)
    p1 = draft.execute_pick(
        season.season_id, entries[0].season_entry_id, players[0].season_player_id, completed_at="2027-01-01"
    )
    draft.execute_pick(
        season.season_id, entries[1].season_entry_id, players[1].season_player_id, completed_at="2027-01-02"
    )

    with pytest.raises(DraftCorrectionError, match="most recently completed"):
        draft.correct_pick(season.season_id, p1.draft_pick_id, corrected_at="2027-01-02T06:00:00")


def test_correcting_an_already_corrected_or_uncompleted_pick_is_rejected():
    _db, season, entries, players, _ownership, _pool, draft = domain(entries=2, limit=2)
    p1 = draft.execute_pick(
        season.season_id, entries[0].season_entry_id, players[0].season_player_id, completed_at="2027-01-01"
    )
    draft.correct_pick(season.season_id, p1.draft_pick_id, corrected_at="2027-01-01T06:00:00")

    with pytest.raises(DraftCorrectionError, match="already been corrected"):
        draft.correct_pick(season.season_id, p1.draft_pick_id, corrected_at="2027-01-01T07:00:00")

    uncompleted = draft.next_pick(season.season_id)
    with pytest.raises(DraftCorrectionError, match="only a completed pick"):
        draft.correct_pick(season.season_id, uncompleted.draft_pick_id)


def test_correction_is_rejected_once_the_draft_is_finalized():
    _db, season, entries, players, _ownership, _pool, draft = domain(entries=2, limit=1)
    p1 = draft.execute_pick(season.season_id, entries[0].season_entry_id, players[0].season_player_id)
    draft.execute_pick(season.season_id, entries[1].season_entry_id, players[1].season_player_id)
    draft.finalize(season.season_id)

    with pytest.raises(DraftFinalizedError):
        draft.correct_pick(season.season_id, p1.draft_pick_id, reason="too late")


def test_correction_is_rejected_if_the_pick_ownership_moved_out_of_band():
    """A correction must undo *this pick's* acquisition specifically -- not
    whatever ownership period happens to be open for the player now. If the
    player was released or transferred by something other than this pick
    since it completed, blindly releasing "the" open period would either
    fail confusingly or release an unrelated entry's acquisition."""
    _db, season, entries, players, ownership, _pool, draft = domain(entries=3, limit=2)
    p1 = draft.execute_pick(
        season.season_id, entries[0].season_entry_id, players[0].season_player_id, completed_at="2027-01-01"
    )

    # The picked player is transferred away from the drafting entry by an
    # unrelated ownership operation before the pick is corrected.
    ownership.transfer(players[0].season_player_id, entries[2].season_entry_id, effective_at="2027-01-05")

    with pytest.raises(DraftCorrectionError, match="ownership has changed"):
        draft.correct_pick(season.season_id, p1.draft_pick_id, reason="too late", corrected_at="2027-01-06")

    # Neither the pick nor the (now unrelated) transferred ownership moved.
    assert draft.picks(season.season_id)[0].draft_pick_id == p1.draft_pick_id
    assert ownership.owner_at(players[0].season_player_id, "2027-01-06").season_entry_id == entries[2].season_entry_id


# -- Finalisation ------------------------------------------------------------


def test_finalize_is_blocked_until_every_pick_is_completed():
    _db, season, entries, players, _ownership, _pool, draft = domain(entries=2, limit=2)
    draft.execute_pick(season.season_id, entries[0].season_entry_id, players[0].season_player_id)

    with pytest.raises(DraftNotCompleteError, match="1/4"):
        draft.finalize(season.season_id)
    assert draft.status(season.season_id).is_finalized is False


def test_finalize_is_blocked_if_a_completed_picks_ownership_was_released_out_of_band():
    """Defence in depth: every draft_pick can show completed_at set and the
    right pick-count per entry while an entry's *actual* active squad is
    short, if something released a player's ownership without going through
    the draft (e.g. OwnershipRepository.release called directly). Counting
    picks alone would miss this; finalize must count live ownership."""
    _db, season, entries, players, ownership, _pool, draft = domain(entries=2, limit=2)
    draft.execute_pick(
        season.season_id, entries[0].season_entry_id, players[0].season_player_id, completed_at="2027-01-01"
    )
    draft.execute_pick(
        season.season_id, entries[1].season_entry_id, players[1].season_player_id, completed_at="2027-01-02"
    )
    draft.execute_pick(
        season.season_id, entries[1].season_entry_id, players[2].season_player_id, completed_at="2027-01-03"
    )
    draft.execute_pick(
        season.season_id, entries[0].season_entry_id, players[3].season_player_id, completed_at="2027-01-04"
    )
    assert draft.status(season.season_id).is_complete

    ownership.release(players[0].season_player_id, effective_at="2027-01-05")

    with pytest.raises(DraftNotCompleteError, match="squad size"):
        draft.finalize(season.season_id)


def test_finalize_succeeds_once_complete_and_blocks_ordinary_mutation_after():
    _db, season, entries, players, _ownership, _pool, draft = domain(entries=2, limit=1)
    p1 = draft.execute_pick(season.season_id, entries[0].season_entry_id, players[0].season_player_id)
    draft.execute_pick(season.season_id, entries[1].season_entry_id, players[1].season_player_id)

    status = draft.finalize(season.season_id, note="draft complete")
    assert status.is_finalized and status.finalized_note == "draft complete"

    with pytest.raises(DraftFinalizedError):
        draft.execute_pick(season.season_id, entries[0].season_entry_id, players[2].season_player_id)
    with pytest.raises(DraftFinalizedError):
        draft.pause(season.season_id)
    with pytest.raises(DraftFinalizedError):
        draft.correct_pick(season.season_id, p1.draft_pick_id)

    # History, ownership and sequencing all remain intact post-finalisation.
    assert len(draft.picks(season.season_id)) == 2
    assert all(pick.completed_at for pick in draft.picks(season.season_id))


def test_reopen_requires_an_explicit_reason_and_only_applies_to_a_finalized_draft():
    _db, season, entries, players, _ownership, _pool, draft = domain(entries=2, limit=1)
    draft.execute_pick(season.season_id, entries[0].season_entry_id, players[0].season_player_id)
    draft.execute_pick(season.season_id, entries[1].season_entry_id, players[1].season_player_id)

    with pytest.raises(DraftStateError, match="not finalized"):
        draft.reopen(season.season_id, reason="cannot reopen what isn't finalized")

    draft.finalize(season.season_id)
    with pytest.raises(ValueError, match="explicit reason"):
        draft.reopen(season.season_id, reason="")

    reopened_status = draft.reopen(season.season_id, reason="scorer error found post-finalisation")
    assert reopened_status.is_finalized is False

    events = AuditEventRepository(_db).list_events(action="draft.reopened")
    assert len(events) == 1 and events[0].reason == "scorer error found post-finalisation"

    # Once reopened, an ordinary finalize can run again (a fresh sign-off).
    status = draft.finalize(season.season_id, note="re-finalised")
    assert status.is_finalized and status.finalized_note == "re-finalised"
