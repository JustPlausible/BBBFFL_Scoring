"""Weekly drafts are private mutable work; submissions are official history."""

import pytest
from sqlalchemy.exc import DatabaseError

from app.competition_lifecycle import CompetitionLifecycleRepository
from app.lineups import LineupConflictError, LineupIntegrityError, POSITIONS, WeeklyLineupRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import operational


def context(year=2027):
    db = migrated_connection()
    lifecycle, round_, entries = operational(db, year, year)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute("SELECT c.season_id, c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?", (round_.bbbffl_round_id,)).fetchone()
    pool, ownership = PlayerPoolRepository(db), OwnershipRepository(db)
    ownership.configure_squad_limit(scope["season_id"], 20)
    players = []
    for number in range(1, 11):
        player = pool.refresh_player(scope["season_id"], year * 100 + number, f"Player {number}")
        ownership.acquire(player.season_player_id, entries[0].season_entry_id)
        players.append(player)
    foreign = pool.refresh_player(scope["season_id"], year * 100 + 99, "Foreign")
    ownership.acquire(foreign.season_player_id, entries[1].season_entry_id)
    return db, lifecycle, round_, entries, scope, players, foreign


def save(repo, round_, entries, scope, positions, revision=0):
    return repo.save_draft(scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id, positions, expected_revision=revision)


def test_draft_repeated_edits_conflict_and_survive_new_repository():
    db, _, round_, entries, scope, players, _ = context()
    repo = WeeklyLineupRepository(db)
    empty = save(repo, round_, entries, scope, {})
    assert empty.revision == 1 and list(empty.positions) == list(POSITIONS)
    edited = save(repo, round_, entries, scope, {"F1": players[0].season_player_id, "F2": players[1].season_player_id}, 1)
    reordered = save(repo, round_, entries, scope, {"F1": players[1].season_player_id, "F2": players[0].season_player_id}, 2)
    assert WeeklyLineupRepository(db).get_draft(scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id) == reordered
    with pytest.raises(LineupConflictError, match="stale"):
        save(repo, round_, entries, scope, {}, edited.revision)


def test_submission_is_immutable_resubmission_history_and_ownership_history_survives():
    db, _, round_, entries, scope, players, _ = context()
    repo = WeeklyLineupRepository(db)
    draft = save(repo, round_, entries, scope, {"F1": players[0].season_player_id})
    first = repo.submit(draft.lineup_id, expected_draft_revision=1, expected_submission_version=0)
    draft2 = save(repo, round_, entries, scope, {"M1": players[1].season_player_id}, 1)
    assert repo.get_submission(draft.lineup_id, 1) == first
    second = repo.submit(draft.lineup_id, expected_draft_revision=2, expected_submission_version=1)
    assert repo.get_effective_submission(draft.lineup_id) == second
    assert repo.get_submission(draft.lineup_id, 1).positions["F1"] == players[0].season_player_id
    OwnershipRepository(db).release(players[0].season_player_id)
    assert repo.get_submission(draft.lineup_id, 1) == first
    with pytest.raises(DatabaseError, match="immutable"):
        with db.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE weekly_lineup_submission SET reason='changed'")


def test_duplicate_foreign_player_lifecycle_and_stale_submission_are_rejected():
    db, lifecycle, round_, entries, scope, players, foreign = context()
    repo = WeeklyLineupRepository(db)
    with pytest.raises(LineupIntegrityError, match="multiple"):
        save(repo, round_, entries, scope, {"F1": players[0].season_player_id, "F2": players[0].season_player_id})
    draft = save(repo, round_, entries, scope, {"F1": foreign.season_player_id})
    with pytest.raises(LineupIntegrityError, match="not currently owned"):
        repo.submit(draft.lineup_id, expected_draft_revision=1, expected_submission_version=0)
    draft = save(repo, round_, entries, scope, {"F1": players[0].season_player_id}, 1)
    first = repo.submit(draft.lineup_id, expected_draft_revision=2, expected_submission_version=0)
    with pytest.raises(LineupConflictError, match="stale submission"):
        repo.submit(draft.lineup_id, expected_draft_revision=2, expected_submission_version=0)
    lifecycle.transition(round_.bbbffl_round_id, "live")
    with pytest.raises(LineupIntegrityError, match="does not currently permit"):
        repo.submit(draft.lineup_id, expected_draft_revision=2, expected_submission_version=first.version)


def test_equal_round_numbers_are_isolated_by_season():
    db = migrated_connection()
    lifecycle1, round1, entries1 = operational(db, 2026, 1)
    lifecycle2, round2, entries2 = operational(db, 2027, 1)
    lifecycle1.transition(round1.bbbffl_round_id, "open")
    lifecycle2.transition(round2.bbbffl_round_id, "open")
    scope1 = db.execute("SELECT c.season_id, c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?", (round1.bbbffl_round_id,)).fetchone()
    scope2 = db.execute("SELECT c.season_id, c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?", (round2.bbbffl_round_id,)).fetchone()
    repo = WeeklyLineupRepository(db)
    d1 = save(repo, round1, entries1, scope1, {})
    d2 = save(repo, round2, entries2, scope2, {})
    assert d1.season_id != d2.season_id and d1.lineup_id != d2.lineup_id
