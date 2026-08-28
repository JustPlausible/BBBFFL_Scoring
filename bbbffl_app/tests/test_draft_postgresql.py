"""PostgreSQL-specific draft invariants enforced by locks and triggers."""

import itertools
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from app.db import connect
from app.draft import DraftPausedError, DraftRepository, DraftTurnError
from app.identity import IdentityRepository
from app.migrations import migrate
from app.player_pool import (
    OwnershipRepository,
    PlayerPoolRepository,
    PlayerUnavailableError,
    SquadConfigurationFrozenError,
)
from app.season import SeasonRepository


@pytest.fixture(scope="module")
def postgres_draft():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL draft semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    database = connect(url)
    # 2201/2202 collide with tests/test_lineups_concurrency.py's own
    # PostgreSQL season years when both files run against the same
    # database in one CI job (see ci.yml's postgres-migrations job) -- 2205
    # is unused by any other PostgreSQL-marked test file.
    season = SeasonRepository(database).create_season(2205, "2205 draft trigger")
    identities = IdentityRepository(database)
    entries = []
    for number in range(2):
        coach = identities.create_coach(f"Draft trigger coach {number}")
        entries.append(
            identities.create_entry(
                season.season_id,
                f"draft-trigger-{number}",
                coach.coach_id,
                f"Draft trigger team {number}",
            )
        )
    ownership = OwnershipRepository(database)
    ownership.configure_squad_limit(season.season_id, 1)
    player = PlayerPoolRepository(database).refresh_player(
        season.season_id,
        2201001,
        "Draft trigger player",
    )
    draft = DraftRepository(database)
    draft.accept_order(
        season.season_id,
        [entry.season_entry_id for entry in entries],
    )
    completed = draft.execute_pick(
        season.season_id,
        entries[0].season_entry_id,
        player.season_player_id,
    )
    yield database, season, entries, ownership, draft, completed
    database.close()


def test_postgresql_accepted_draft_freezes_squad_capacity(postgres_draft):
    database, season, entries, ownership, draft, _completed = postgres_draft

    ownership.configure_squad_limit(season.season_id, 1)
    with pytest.raises(SquadConfigurationFrozenError, match="cannot change"):
        ownership.configure_squad_limit(season.season_id, 2)

    assert len(draft.picks(season.season_id)) == len(entries)
    configuration = database.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?",
        (season.season_id,),
    ).fetchone()
    accepted = database.execute(
        "SELECT target_squad_size FROM season_draft WHERE season_id=?",
        (season.season_id,),
    ).fetchone()
    assert configuration["squad_limit"] == accepted["target_squad_size"] == 1


def test_postgresql_rejects_deleting_completed_pick(postgres_draft):
    database, _season, _entries, _ownership, _draft, completed = postgres_draft

    with pytest.raises(ProgrammingError, match="accepted/completed draft history is immutable"):
        with database.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM draft_pick WHERE draft_pick_id=:pick_id"),
                {"pick_id": completed.draft_pick_id},
            )


_contested_pick_years = itertools.count(2210)


@pytest.fixture
def contested_pick():
    """A fresh two-entry, one-round draft for exactly one contested pick --
    module-scoped `postgres_draft` above already has its only pick
    completed, so concurrency tests need their own isolated draft. Each use
    gets its own season year (`bbbffl_season.year` is unique) since this
    fixture is function-scoped and several tests use it."""
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL draft concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    database = connect(url)
    year = next(_contested_pick_years)
    season = SeasonRepository(database).create_season(year, f"{year} draft concurrency")
    identities = IdentityRepository(database)
    entries = [
        identities.create_entry(
            season.season_id,
            f"draft-concurrency-{number}",
            identities.create_coach(f"Concurrency coach {number}").coach_id,
            f"Concurrency team {number}",
        )
        for number in range(2)
    ]
    ownership = OwnershipRepository(database)
    ownership.configure_squad_limit(season.season_id, 1)
    player = PlayerPoolRepository(database).refresh_player(season.season_id, 2202001, "Contested player")
    draft = DraftRepository(database)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    yield database, season, entries, ownership, draft, player
    database.close()


def test_postgresql_two_concurrent_attempts_at_the_same_pick_exactly_one_succeeds(contested_pick):
    database, season, entries, ownership, draft, player = contested_pick
    ready = threading.Barrier(2)

    def attempt():
        ready.wait(timeout=5)
        try:
            return draft.execute_pick(season.season_id, entries[0].season_entry_id, player.season_player_id)
        except (DraftTurnError, PlayerUnavailableError, IntegrityError, OperationalError):
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _n: attempt(), range(2)))

    assert sum(result is not None for result in results) == 1
    completed = [pick for pick in draft.picks(season.season_id) if pick.completed_at]
    assert len(completed) == 1
    assert len(ownership.history(player.season_player_id)) == 1


def test_postgresql_pause_waits_for_the_season_draft_row_lock(contested_pick):
    """Proves pause() uses the same PostgreSQL row-lock discipline as pick
    execution (`_locked_draft`'s `SELECT ... FOR UPDATE`) rather than a
    race-prone in-process flag: a concurrent pause() must queue behind a
    held lock on `season_draft`, not interleave with it."""
    database, season, entries, _ownership, draft, player = contested_pick
    ready_to_pause = threading.Event()

    def attempt_pause():
        ready_to_pause.wait(timeout=5)
        return draft.pause(season.season_id, reason="race check")

    # A plain Thread, not `with ThreadPoolExecutor(...) as executor:` --
    # that context manager's __exit__ joins the worker before this scope
    # ends, which would deadlock here: attempt_pause() cannot return until
    # the lock below is released, and the lock isn't released until this
    # `with` block's body (which starts attempt_pause) finishes.
    pause_thread = threading.Thread(target=attempt_pause)
    with database.engine.connect() as blocker_connection:
        with blocker_connection.begin():
            blocker_connection.execute(
                text("SELECT * FROM season_draft WHERE season_id=:season_id FOR UPDATE"),
                {"season_id": season.season_id},
            )
            pause_thread.start()
            ready_to_pause.set()
            time.sleep(0.2)
            assert pause_thread.is_alive(), "pause() did not wait for the season_draft row lock"
        # blocker_connection's transaction ends here (releasing the lock).
    pause_thread.join(timeout=5)
    assert not pause_thread.is_alive(), "pause() never completed after the lock was released"

    assert draft.status(season.season_id).is_paused
    with pytest.raises(DraftPausedError):
        draft.execute_pick(season.season_id, entries[0].season_entry_id, player.season_player_id)
    draft.resume(season.season_id)
    completed = draft.execute_pick(season.season_id, entries[0].season_entry_id, player.season_player_id)
    assert completed.selected_season_player_id == player.season_player_id


def test_postgresql_correction_reopens_slot_and_preserves_the_original_row(postgres_draft):
    database, season, entries, ownership, draft, completed = postgres_draft

    reopened = draft.correct_pick(season.season_id, completed.draft_pick_id, reason="postgres correction check")
    assert reopened.completed_at is None
    assert draft.next_pick(season.season_id) == reopened

    original_row = next(
        pick
        for pick in draft.picks(season.season_id, include_superseded=True)
        if pick.draft_pick_id == completed.draft_pick_id
    )
    assert original_row.selected_season_player_id == completed.selected_season_player_id
    assert original_row.superseded_by_draft_pick_id == reopened.draft_pick_id

    with pytest.raises(ProgrammingError, match="accepted/completed draft history is immutable"):
        with database.engine.begin() as connection:
            connection.execute(
                text("UPDATE draft_pick SET selected_season_player_id=NULL WHERE draft_pick_id=:pick_id"),
                {"pick_id": completed.draft_pick_id},
            )
