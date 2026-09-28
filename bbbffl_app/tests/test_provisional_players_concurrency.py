"""Issue #242 (Codex review on PR #258, P1, eighth round): PostgreSQL
serialization between candidate detection's canonical-row quarantine and a
concurrent draft-pick acquisition of that same row. `_detect_candidates_
for_provisional`'s matched-row lookup now takes a row lock so a concurrent
`OwnershipRepository.acquire_in_transaction` acquisition cannot slip in
between the read and the quarantine write. Skipped unless
`BBBFFL_DATABASE_URL` points at PostgreSQL (the CI postgres job)."""

from threading import Thread

from sqlalchemy import text

from app.audit import ActorContext
from app.db import connect
from app.player_pool import PlayerPoolRepository
from app.provisional_players import ProvisionalPlayerRepository, detect_candidates
from tests.season_setup_helpers import fresh_season
from tests.test_replay_bootstrap_concurrency import postgres_url  # noqa: F401 -- fixture

SCORER = ActorContext.anonymous_operator("scorer")
REASON = "issue #242 concurrency test"


def test_candidate_detection_blocks_behind_a_concurrently_locked_matching_row(postgres_url):  # noqa: F811
    """Detection's own matched-row lock (this fix) must serialize behind
    whatever else is holding that row -- here, a stand-in for `Ownership
    Repository.acquire_in_transaction`'s row lock -- rather than reading a
    stale `eligible=TRUE` and quarantining an already-acquired row out from
    under a concurrent draft pick, which would leave the new provisional
    identity eligible while `reconcile` later refuses the target for having
    ownership history."""
    database = connect(postgres_url)
    season, _entries = fresh_season(database)
    ProvisionalPlayerRepository(database).create(
        season.season_id,
        display_name="Jordan Newrecruit",
        given_name="Jordan",
        family_name="Newrecruit",
        note="verified via club website squad list",
        actor=SCORER,
        reason=REASON,
    )
    pool = PlayerPoolRepository(database)
    canonical = pool.refresh_player(
        season.season_id, 9901, "Jordan Newrecruit", given_name="Jordan", family_name="Newrecruit"
    )
    # Not yet detected -- `refresh_player` alone never runs detection.
    assert pool.get_by_id(canonical.season_player_id).eligible is True

    lock_conn = connect(postgres_url).engine.connect()
    lock_txn = lock_conn.begin()
    lock_conn.execute(
        text("SELECT * FROM season_player_pool WHERE season_player_id=:id FOR UPDATE"),
        {"id": canonical.season_player_id},
    )

    outcome = {}

    def run_detection():
        db = connect(postgres_url)
        try:
            outcome["detected"] = detect_candidates(db, season.season_id, actor=SCORER)
        finally:
            db.close()

    thread = Thread(target=run_detection)
    thread.start()
    thread.join(timeout=1)
    assert thread.is_alive(), "candidate detection did not wait for the concurrently held matching row's lock"
    lock_txn.commit()
    lock_conn.close()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["detected"] == 1
    assert pool.get_by_id(canonical.season_player_id).eligible is False
