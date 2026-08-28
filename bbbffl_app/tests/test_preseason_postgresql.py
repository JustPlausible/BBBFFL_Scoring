"""PostgreSQL-specific preseason window/trade concurrency invariants
(roadmap package 15, issue #54) -- proves two conflicting trades over the
same player cannot both succeed, and that closing the window uses the same
row-lock discipline as opening it, mirroring
tests/test_draft_postgresql.py's proofs for the draft ledger."""

import itertools
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.db import connect
from app.draft import DraftRepository
from app.identity import IdentityRepository
from app.migrations import migrate
from app.player_pool import OwnershipRepository, PlayerPoolRepository, PlayerUnavailableError, SquadCapacityError
from app.preseason import PreseasonRepository, PreseasonTradeValidationError, PreseasonWindowClosedError
from app.season import SeasonRepository

_contested_trade_years = itertools.count(2240)


@pytest.fixture
def contested_trade():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL preseason concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    database = connect(url)
    year = next(_contested_trade_years)
    season = SeasonRepository(database).create_season(year, f"{year} preseason concurrency")
    identities = IdentityRepository(database)
    entries = [
        identities.create_entry(
            season.season_id,
            f"preseason-concurrency-{year}-{n}",
            identities.create_coach(f"Concurrency coach {year}-{n}").coach_id,
            f"Concurrency team {year}-{n}",
        )
        for n in range(3)
    ]
    ownership = OwnershipRepository(database)
    ownership.configure_squad_limit(season.season_id, 2)
    pool = PlayerPoolRepository(database)
    players = [pool.refresh_player(season.season_id, year * 10 + n, f"Contested player {year}-{n}") for n in range(6)]
    draft = DraftRepository(database)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    for _ in range(6):
        pick = draft.next_pick(season.season_id)
        draft.execute_pick(
            season.season_id, pick.current_season_entry_id, players[pick.overall_number - 1].season_player_id
        )
    draft.finalize(season.season_id)
    preseason = PreseasonRepository(database)
    preseason.open_window(season.season_id)
    yield database, season, entries, ownership, preseason, players
    database.close()


def test_postgresql_two_concurrent_trades_for_the_same_player_exactly_one_succeeds(contested_trade):
    """Two scorer sessions each try to trade the same player (currently
    owned by entry 0) to a *different* destination entry at the same time.
    The ownership ledger's row locks/overlap trigger must ensure exactly
    one of these atomic trades commits."""
    database, season, entries, ownership, preseason, players = contested_trade
    e0, e1, e2 = (entry.season_entry_id for entry in entries)
    contested_player = players[0].season_player_id
    # Release each destination entry's second player, leaving room for
    # exactly one more -- otherwise a destination entry already at its
    # configured capacity would make *every* trade attempt below fail with
    # SquadCapacityError regardless of the ownership race this test targets.
    for entry in (e1, e2):
        spare = ownership.squad_at(entry, "9999-01-01")[1]
        ownership.release(spare.season_player_id)
    ready = threading.Barrier(2)

    def attempt(destination):
        ready.wait(timeout=5)
        try:
            return preseason.submit_trade(
                season.season_id,
                [{"season_player_id": contested_player, "from_season_entry_id": e0, "to_season_entry_id": destination}],
            )
        except (
            PreseasonTradeValidationError,
            PlayerUnavailableError,
            SquadCapacityError,
            IntegrityError,
            OperationalError,
        ):
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, [e1, e2]))

    assert sum(result is not None for result in results) == 1
    history = ownership.history(contested_player)
    active = [period for period in history if period.released_at is None]
    assert len(active) == 1
    assert active[0].season_entry_id in (e1, e2)
    assert len(preseason.list_trades(season.season_id)) == 1


def test_postgresql_close_window_waits_for_the_window_row_lock(contested_trade):
    """`close_window` must queue behind a held lock on
    `season_preseason_window` rather than racing a concurrent close/trade --
    the same `_locked_window` (`SELECT ... FOR UPDATE`) discipline
    `test_postgresql_pause_waits_for_the_season_draft_row_lock` already
    proves for the draft's `season_draft` row."""
    database, season, _entries, _ownership, preseason, _players = contested_trade
    ready_to_close = threading.Event()

    def attempt_close():
        ready_to_close.wait(timeout=5)
        return preseason.close_window(season.season_id, reason="race check")

    close_thread = threading.Thread(target=attempt_close)
    with database.engine.connect() as blocker_connection:
        with blocker_connection.begin():
            blocker_connection.execute(
                text("SELECT * FROM season_preseason_window WHERE season_id=:season_id FOR UPDATE"),
                {"season_id": season.season_id},
            )
            close_thread.start()
            ready_to_close.set()
            time.sleep(0.2)
            assert close_thread.is_alive(), "close_window() did not wait for the season_preseason_window row lock"
        # blocker_connection's transaction ends here (releasing the lock).
    close_thread.join(timeout=5)
    assert not close_thread.is_alive(), "close_window() never completed after the lock was released"

    assert not preseason.get_window(season.season_id).is_open
    with pytest.raises(PreseasonWindowClosedError):
        preseason.close_window(season.season_id)
