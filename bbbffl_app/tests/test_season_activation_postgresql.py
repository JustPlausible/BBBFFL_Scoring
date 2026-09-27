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
