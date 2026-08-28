"""Property-style and transactional coverage for the preseason draft ledger."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.draft import DraftOrderError, DraftPickCompletedError, DraftRepository, DraftTurnError, snake_allocations
from app.identity import IdentityRepository
from app.player_pool import (
    OwnershipRepository,
    PlayerPoolRepository,
    PlayerUnavailableError,
    SquadCapacityError,
    SquadConfigurationFrozenError,
)
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


def domain(entries=4, limit=2, players=12):
    db = migrated_connection()
    season = SeasonRepository(db).create_season(2027, "2027")
    identities = IdentityRepository(db)
    season_entries = []
    for number in range(entries):
        coach = identities.create_coach(f"Coach {number}")
        season_entries.append(
            identities.create_entry(season.season_id, f"licence-{number}", coach.coach_id, f"Team {number}")
        )
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(season.season_id, limit)
    pool = PlayerPoolRepository(db)
    season_players = [
        pool.refresh_player(season.season_id, number + 1, f"Player {number}") for number in range(players)
    ]
    return db, season, season_entries, season_players, ownership, DraftRepository(db)


@pytest.mark.parametrize("entry_count", [2, 3, 4, 10])
@pytest.mark.parametrize("target", [1, 2, 5])
def test_snake_sequence_properties(entry_count, target):
    entries = [f"entry-{number}" for number in range(entry_count)]
    picks = list(snake_allocations(entries, target))
    assert len(picks) == entry_count * target
    assert [pick[0] for pick in picks] == list(range(1, len(picks) + 1))
    for round_number in range(1, target + 1):
        round_picks = picks[(round_number - 1) * entry_count : round_number * entry_count]
        expected = entries if round_number % 2 else list(reversed(entries))
        assert [pick[3] for pick in round_picks] == expected
        assert {pick[3] for pick in round_picks} == set(entries)
    if target > 1:
        assert picks[entry_count - 1][3] == picks[entry_count][3]


def test_ten_entry_order_is_complete_frozen_and_materialised():
    db, season, entries, _players, _ownership, draft = domain(entries=10, limit=3)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    assert len(draft.picks(season.season_id)) == 30
    with pytest.raises(DraftOrderError, match="frozen"):
        draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    with pytest.raises(Exception, match="immutable"):
        with db.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE draft_order_position SET position=position")


def test_accepted_draft_freezes_squad_capacity_but_same_value_is_safe():
    db, season, entries, _players, ownership, draft = domain(entries=4, limit=3)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])

    ownership.configure_squad_limit(season.season_id, 3)
    with pytest.raises(SquadConfigurationFrozenError, match="cannot change"):
        ownership.configure_squad_limit(season.season_id, 4)

    configured = db.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?",
        (season.season_id,),
    ).fetchone()
    accepted = db.execute(
        "SELECT target_squad_size FROM season_draft WHERE season_id=?",
        (season.season_id,),
    ).fetchone()
    assert configured["squad_limit"] == accepted["target_squad_size"] == 3
    assert len(draft.picks(season.season_id)) == len(entries) * 3


@pytest.mark.parametrize("bad", [lambda ids: ids[:-1], lambda ids: ids[:-1] + ids[:1]])
def test_incomplete_or_duplicate_order_is_rejected(bad):
    _db, season, entries, _players, _ownership, draft = domain()
    ids = [entry.season_entry_id for entry in entries]
    with pytest.raises(DraftOrderError):
        draft.accept_order(season.season_id, bad(ids))
    assert draft.picks(season.season_id) == []


def test_transfer_preserves_allocation_sequence_and_history():
    _db, season, entries, _players, _ownership, draft = domain()
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    before = draft.picks(season.season_id)[1]
    draft.transfer_pick(before.draft_pick_id, entries[0].season_entry_id, reason="trade T-7")
    after = draft.picks(season.season_id)[1]
    assert after.original_season_entry_id == before.original_season_entry_id
    assert after.current_season_entry_id == entries[0].season_entry_id
    assert after.overall_number == before.overall_number
    history = draft.transfer_history(after.draft_pick_id)
    assert [(item.from_season_entry_id, item.to_season_entry_id, item.reason) for item in history] == [
        (entries[1].season_entry_id, entries[0].season_entry_id, "trade T-7")
    ]


def test_execution_creates_ownership_and_allows_snake_consecutive_picks():
    _db, season, entries, players, ownership, draft = domain(entries=2, limit=2)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    draft.execute_pick(
        season.season_id, entries[0].season_entry_id, players[0].season_player_id, completed_at="2027-01-01"
    )
    draft.execute_pick(
        season.season_id, entries[1].season_entry_id, players[1].season_player_id, completed_at="2027-01-02"
    )
    draft.execute_pick(
        season.season_id, entries[1].season_entry_id, players[2].season_player_id, completed_at="2027-01-03"
    )
    assert ownership.owner_at(players[2].season_player_id, "2027-01-04").season_entry_id == entries[1].season_entry_id


def test_traded_adjacent_pick_allows_same_owner_twice():
    _db, season, entries, players, _ownership, draft = domain(entries=3, limit=2)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    second = draft.picks(season.season_id)[1]
    draft.transfer_pick(second.draft_pick_id, entries[0].season_entry_id)
    draft.execute_pick(season.season_id, entries[0].season_entry_id, players[0].season_player_id)
    draft.execute_pick(season.season_id, entries[0].season_entry_id, players[1].season_player_id)
    assert [pick.current_season_entry_id for pick in draft.picks(season.season_id)[:2]] == [
        entries[0].season_entry_id
    ] * 2


def test_wrong_turn_owner_and_owned_player_fail_without_partial_draft_write():
    _db, season, entries, players, ownership, draft = domain()
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    first = draft.picks(season.season_id)[0]
    with pytest.raises(DraftTurnError):
        draft.execute_pick(season.season_id, entries[1].season_entry_id, players[0].season_player_id)
    assert draft.next_pick(season.season_id) == first
    assert ownership.history(players[0].season_player_id) == []
    ownership.acquire(players[0].season_player_id, entries[1].season_entry_id, effective_at="2026-12-01")
    with pytest.raises(PlayerUnavailableError):
        draft.execute_pick(season.season_id, entries[0].season_entry_id, players[0].season_player_id)
    assert draft.next_pick(season.season_id) == first


def test_capacity_and_completed_pick_rejections_are_atomic():
    _db, season, entries, players, ownership, draft = domain(entries=2, limit=1)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    ownership.acquire(players[0].season_player_id, entries[0].season_entry_id, effective_at="2026-12-01")
    first = draft.next_pick(season.season_id)
    with pytest.raises(SquadCapacityError):
        draft.execute_pick(season.season_id, entries[0].season_entry_id, players[1].season_player_id)
    assert draft.next_pick(season.season_id) == first
    assert ownership.history(players[1].season_player_id) == []

    # A separate draft demonstrates explicit replay protection.
    _db, season, entries, players, ownership, draft = domain(entries=2, limit=1)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    completed = draft.execute_pick(season.season_id, entries[0].season_entry_id, players[0].season_player_id)
    with pytest.raises(DraftPickCompletedError):
        draft.execute_pick(
            season.season_id,
            entries[0].season_entry_id,
            players[1].season_player_id,
            pick_id=completed.draft_pick_id,
        )
    assert len([pick for pick in draft.picks(season.season_id) if pick.completed_at]) == 1
    assert ownership.history(players[1].season_player_id) == []


def test_database_rejects_deleting_a_completed_pick_but_not_an_uncompleted_pick():
    db, season, entries, players, _ownership, draft = domain(entries=2, limit=2)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    completed = draft.execute_pick(
        season.season_id,
        entries[0].season_entry_id,
        players[0].season_player_id,
    )
    uncompleted = draft.picks(season.season_id)[-1]

    with pytest.raises(IntegrityError, match="completed draft pick is immutable"):
        with db.engine.begin() as conn:
            conn.exec_driver_sql(
                "DELETE FROM draft_pick WHERE draft_pick_id=?",
                (completed.draft_pick_id,),
            )
    with db.engine.begin() as conn:
        conn.exec_driver_sql(
            "DELETE FROM draft_pick WHERE draft_pick_id=?",
            (uncompleted.draft_pick_id,),
        )


def test_completed_history_uses_durable_ids_after_names_and_roster_change():
    db, season, entries, players, ownership, draft = domain(entries=2, limit=2)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    completed = draft.execute_pick(
        season.season_id, entries[0].season_entry_id, players[0].season_player_id, completed_at="2027-01-01"
    )
    with db.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE season_entry_team_name_history SET team_name='Renamed' WHERE season_entry_id=? AND ended_at IS NULL",
            (entries[0].season_entry_id,),
        )
        conn.exec_driver_sql(
            "UPDATE season_player_pool SET display_name='Renamed player' WHERE season_player_id=?",
            (players[0].season_player_id,),
        )
    ownership.transfer(players[0].season_player_id, entries[1].season_entry_id, effective_at="2027-02-01")
    persisted = draft.picks(season.season_id)[0]
    assert persisted.draft_pick_id == completed.draft_pick_id
    assert persisted.original_season_entry_id == entries[0].season_entry_id
    assert persisted.selected_season_player_id == players[0].season_player_id


def test_competing_execution_completes_one_pick_and_player_once():
    _db, season, entries, players, ownership, draft = domain(entries=2, limit=1)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])

    def attempt():
        try:
            return draft.execute_pick(season.season_id, entries[0].season_entry_id, players[0].season_player_id)
        except (DraftTurnError, PlayerUnavailableError, IntegrityError, OperationalError):
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _number: attempt(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert len([pick for pick in draft.picks(season.season_id) if pick.completed_at]) == 1
    assert len(ownership.history(players[0].season_player_id)) == 1
