from types import SimpleNamespace

from app.coach_lineup import CoachLineupService
from app.lineups import POSITIONS
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import operational


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
