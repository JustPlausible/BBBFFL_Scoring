"""Issue #239 (Codex review, PR #254, P2): `activate_season` must lock each
readiness check's own prerequisite rows through the same transaction as the
lifecycle transition, so a concurrent write to one of them serializes
against activation rather than racing an unlocked read. Mirrors
`tests/test_season_setup_concurrency.py`'s
`test_opening_round_acceptance_blocks_behind_a_completing_pick_then_refuses`
-- hold the exact lock the check takes, prove the operation under test
waits for it, then release it and observe the correct outcome. Skipped
unless `BBBFFL_DATABASE_URL` points at PostgreSQL (the CI postgres job)."""

from threading import Thread

from sqlalchemy import text

from app.db import connect
from app.season import SeasonRepository
from app.season_activation import SeasonNotReadyToActivateError, activate_season
from tests.season_activation_helpers import ACTOR, build_activation_ready_season
from tests.test_replay_bootstrap_concurrency import postgres_url  # noqa: F401 -- fixture

REASON = "issue #239 concurrency test"


def test_activation_blocks_behind_a_concurrently_held_draft_lock_then_refuses(postgres_url):  # noqa: F811
    database = connect(postgres_url)
    built = build_activation_ready_season(database, year=9239)
    season_id = built["season"].season_id

    # Hold the exact lock `_draft_check` takes on the season's `season_draft`
    # row -- the same row `DraftRepository.reopen_in_transaction` locks
    # before clearing `finalized_at`.
    lock_conn = connect(postgres_url).engine.connect()
    lock_txn = lock_conn.begin()
    lock_conn.execute(text("SELECT * FROM season_draft WHERE season_id=:sid FOR UPDATE"), {"sid": season_id})

    outcome = {}

    def activate():
        db = connect(postgres_url)
        try:
            activate_season(db, season_id, actor=ACTOR, reason=REASON)
            outcome["result"] = "activated"
        except SeasonNotReadyToActivateError as exc:
            outcome["result"] = str(exc)
        finally:
            db.close()

    thread = Thread(target=activate)
    thread.start()
    thread.join(timeout=1)
    assert thread.is_alive(), "activation did not wait for the concurrently held draft lock"

    # Reopen the draft while activation is blocked on that lock -- once it
    # proceeds, it must observe this change, never a stale "finalized"
    # snapshot read before this commit.
    lock_conn.execute(
        text("UPDATE season_draft SET finalized_at=NULL, finalized_note=NULL WHERE season_id=:sid"),
        {"sid": season_id},
    )
    lock_txn.commit()
    lock_conn.close()

    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "finalized" in outcome["result"]
    assert SeasonRepository(database).get_season(season_id).lifecycle_state == "setup"


def test_activation_locks_the_preseason_window_before_the_draft_avoiding_deadlock(postgres_url):  # noqa: F811
    """Codex review, PR #254 (a second P2): `PreseasonRepository.
    close_window`/`correct_opening_snapshot` lock `season_preseason_window`
    *before* `season_draft`/ownership rows. `_draft_check` must lock in the
    same order -- window first -- so a concurrent close/correction and an
    activation can never each hold what the other wants next. Proven here
    by holding the window row (simulating a concurrent close/correction
    already past its first lock) and confirming activation blocks on it
    immediately, rather than first acquiring `season_draft`/ownership locks
    out of turn and risking a real PostgreSQL deadlock error."""
    database = connect(postgres_url)
    built = build_activation_ready_season(database, year=9240)
    season_id = built["season"].season_id

    lock_conn = connect(postgres_url).engine.connect()
    lock_txn = lock_conn.begin()
    lock_conn.execute(text("SELECT * FROM season_preseason_window WHERE season_id=:sid FOR UPDATE"), {"sid": season_id})

    outcome = {}

    def activate():
        db = connect(postgres_url)
        try:
            activate_season(db, season_id, actor=ACTOR, reason=REASON)
            outcome["result"] = "activated"
        except Exception as exc:  # noqa: BLE001 -- any exception, including a deadlock error, fails the test below
            outcome["result"] = f"{type(exc).__name__}: {exc}"
        finally:
            db.close()

    thread = Thread(target=activate)
    thread.start()
    thread.join(timeout=1)
    assert thread.is_alive(), "activation did not wait for the concurrently held preseason-window lock"

    lock_txn.commit()
    lock_conn.close()

    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["result"] == "activated", outcome["result"]
    assert SeasonRepository(database).get_season(season_id).lifecycle_state == "active"


def test_activation_locks_squad_configuration_before_the_draft_avoiding_deadlock(postgres_url):  # noqa: F811
    """Codex review, PR #254 (a third P2 round): `OwnershipRepository.
    configure_squad_limit` locks `season_squad_configuration` *before*
    `season_draft`. `_evaluate_checks` must lock in the same order --
    squad configuration first -- so a concurrent (redundant, and
    ultimately refused) squad-limit request against an already-drafted
    season can never each hold what the other wants next."""
    database = connect(postgres_url)
    built = build_activation_ready_season(database, year=9241)
    season_id = built["season"].season_id

    lock_conn = connect(postgres_url).engine.connect()
    lock_txn = lock_conn.begin()
    lock_conn.execute(
        text("SELECT * FROM season_squad_configuration WHERE season_id=:sid FOR UPDATE"), {"sid": season_id}
    )

    outcome = {}

    def activate():
        db = connect(postgres_url)
        try:
            activate_season(db, season_id, actor=ACTOR, reason=REASON)
            outcome["result"] = "activated"
        except Exception as exc:  # noqa: BLE001 -- any exception, including a deadlock error, fails the test below
            outcome["result"] = f"{type(exc).__name__}: {exc}"
        finally:
            db.close()

    thread = Thread(target=activate)
    thread.start()
    thread.join(timeout=1)
    assert thread.is_alive(), "activation did not wait for the concurrently held squad-configuration lock"

    lock_txn.commit()
    lock_conn.close()

    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["result"] == "activated", outcome["result"]
    assert SeasonRepository(database).get_season(season_id).lifecycle_state == "active"


def test_activation_never_contends_for_season_entry_or_player_pool_locks(postgres_url):  # noqa: F811
    """Codex review, PR #254 (a fourth P2 round): a stress run reproduced a
    real PostgreSQL deadlock between `activate_season` (which locked
    `season_entry`) and an ordinary `OwnershipRepository.acquire`/`release`
    (which locks `season_player_pool` then, on refusal, `season_preseason_
    window` -- the opposite of this module's window-first order for that
    pair). The fix is to never lock `season_entry`/`season_player_pool`
    here at all (see `_entries_check`'s docstring) -- proven here by
    holding both rows from a separate connection and confirming
    `activate_season` does not even wait for them."""
    database = connect(postgres_url)
    built = build_activation_ready_season(database, year=9242)
    season_id = built["season"].season_id
    entry_id = built["entries"][0].season_entry_id

    lock_conn = connect(postgres_url).engine.connect()
    lock_txn = lock_conn.begin()
    lock_conn.execute(text("SELECT * FROM season_entry WHERE season_entry_id=:eid FOR UPDATE"), {"eid": entry_id})
    lock_conn.execute(
        text("SELECT * FROM season_player_pool WHERE season_id=:sid LIMIT 1 FOR UPDATE"), {"sid": season_id}
    )

    try:
        result = activate_season(database, season_id, actor=ACTOR, reason=REASON)
        assert result.season.lifecycle_state == "active"
    finally:
        lock_txn.rollback()
        lock_conn.close()
