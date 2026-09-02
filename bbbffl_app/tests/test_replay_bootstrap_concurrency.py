"""Production PostgreSQL serialization between the 2026 first-half replay
bootstrap's before-Pick-1 Opening Round reconciliation and concurrent
draft/rule activity (Codex review, PR #127).

Each test below provisions its own uniquely-named PostgreSQL database
(rather than sharing one across the module): a completed draft pick is
immutable by design (0015's append-only trigger), so a test that must
start from zero completed picks cannot reuse a database another test in
this file has already advanced.
"""

import os
import uuid
from datetime import datetime, timezone
from threading import Thread
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import create_engine, text

from app.db import connect
from app.draft import DraftRepository
from app.migrations import migrate
from app.opening_round import OpeningRoundRuleRepository
from app.player_pool import PlayerPoolRepository
from app.replay_bootstrap import ReplayBootstrapError, bootstrap_first_half, load_replay_config
from tests.test_replay_bootstrap import _files


def _admin_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))


@pytest.fixture
def postgres_url():
    """A uniquely-named, freshly migrated PostgreSQL database for exactly
    one test -- created and dropped on the same server as
    `BBBFFL_DATABASE_URL`, so each test's fixed-2026-season bootstrap
    starts from zero completed picks regardless of what any other test in
    this file has done."""
    base_url = os.getenv("BBBFFL_DATABASE_URL")
    if not base_url or not base_url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    db_name = f"bbbffl_conc_{uuid.uuid4().hex[:16]}"
    admin_engine = create_engine(_admin_url(base_url), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()
    parts = urlsplit(base_url)
    url = urlunsplit((parts.scheme, parts.netloc, f"/{db_name}", parts.query, parts.fragment))
    migrate(url)
    yield url
    admin_engine = create_engine(_admin_url(base_url), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
    admin_engine.dispose()


def test_bootstrap_blocks_behind_a_concurrently_completing_pick_then_refuses(tmp_path, postgres_url):
    """`DraftRepository.execute_pick`/`correct_pick` acquire a `SELECT ...
    FOR UPDATE` lock on the season's `season_draft` row before mutating
    any pick (`_locked_draft`). Bootstrap's own before-Pick-1
    completed-picks check must contend for that exact same lock --
    otherwise its "no picks completed yet" decision could be based on a
    snapshot that a concurrently completing pick invalidates moments
    later, letting Opening Round rules be newly established even though
    drafting has, in wall-clock terms, already begun. This test
    deterministically forces that interleaving (rather than racing on
    timing) by holding the lock open across threads, and proves bootstrap
    both blocks behind it and correctly refuses once it observes the
    completed pick."""
    config = load_replay_config(_files(tmp_path))
    setup_db = connect(postgres_url)
    report = bootstrap_first_half(setup_db, config)
    season_id = report["season"]["season_id"]
    player = PlayerPoolRepository(setup_db).list_selectable(season_id)[0]
    # Simulate a season bootstrapped before Opening Round rules existed.
    with setup_db.engine.begin() as raw:
        raw.execute(text("DELETE FROM opening_round_rule_revision"))
        raw.execute(text("DELETE FROM opening_round_rule"))
    setup_db.close()

    # Manually acquire the exact row lock DraftRepository._locked_draft
    # takes before execute_pick mutates any pick, and hold it open across
    # threads -- simulating a pick that has claimed the lock but not yet
    # completed/committed.
    lock_conn = connect(postgres_url).engine.connect()
    lock_txn = lock_conn.begin()
    lock_conn.execute(text("SELECT * FROM season_draft WHERE season_id=:sid FOR UPDATE"), {"sid": season_id})

    bootstrap_db = connect(postgres_url)
    outcome = {}

    def run_bootstrap():
        try:
            bootstrap_first_half(bootstrap_db, config)
            outcome["result"] = "bootstrapped"
        except ReplayBootstrapError:
            outcome["result"] = "refused"

    thread = Thread(target=run_bootstrap)
    thread.start()
    thread.join(timeout=0.5)
    assert thread.is_alive(), (
        "bootstrap did not block on the concurrently held season_draft lock -- "
        "its completed-picks check is not serialized against a concurrent pick"
    )

    # Complete the pick exactly as execute_pick would, then release the lock.
    lock_conn.execute(
        text(
            "UPDATE draft_pick SET selected_season_player_id=:player, completed_at=:at "
            "WHERE draft_pick_id=(SELECT p.draft_pick_id FROM draft_pick p JOIN season_draft d "
            "ON d.draft_id=p.draft_id WHERE d.season_id=:sid AND p.completed_at IS NULL "
            "ORDER BY p.overall_number LIMIT 1)"
        ),
        {"player": player.season_player_id, "at": datetime.now(timezone.utc).isoformat(), "sid": season_id},
    )
    lock_txn.commit()
    lock_conn.close()

    thread.join(timeout=5)
    assert not thread.is_alive(), "bootstrap did not resume after the concurrent pick's lock was released"
    assert outcome["result"] == "refused"
    assert OpeningRoundRuleRepository(bootstrap_db).list_accepted_for_season(season_id) == []
    assert DraftRepository(bootstrap_db).status(season_id).completed_picks == 1
    bootstrap_db.close()


def test_bootstrap_serializes_against_a_concurrent_holder_of_the_season_advisory_lock(tmp_path, postgres_url):
    """Two concurrent invocations of this bootstrap against the same
    season must not both observe a stale "no unexpected rule" snapshot
    and commit -- neither `list_accepted_for_season_locked`'s plain read
    nor per-club inserts for distinct clubs contend for a shared row, so
    without an explicit season-scoped lock this would be a phantom-read
    race (Codex review, PR #127). This test holds the exact PostgreSQL
    advisory lock `_lock_season_for_opening_round_reconciliation` takes,
    proving bootstrap blocks behind a concurrent holder and only proceeds
    (successfully, here) once that lock is released."""
    config = load_replay_config(_files(tmp_path))
    setup_db = connect(postgres_url)
    report = bootstrap_first_half(setup_db, config)
    season_id = report["season"]["season_id"]
    # Simulate a season bootstrapped before Opening Round rules existed,
    # so the rerun below must actually reach reconciliation (not a no-op).
    with setup_db.engine.begin() as raw:
        raw.execute(text("DELETE FROM opening_round_rule_revision"))
        raw.execute(text("DELETE FROM opening_round_rule"))
    setup_db.close()

    lock_conn = connect(postgres_url).engine.connect()
    lock_txn = lock_conn.begin()
    lock_conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:sid))"), {"sid": season_id})

    bootstrap_db = connect(postgres_url)
    outcome = {}

    def run_bootstrap():
        outcome["report"] = bootstrap_first_half(bootstrap_db, config)

    thread = Thread(target=run_bootstrap)
    thread.start()
    thread.join(timeout=0.5)
    assert thread.is_alive(), (
        "bootstrap did not block on the concurrently held season advisory lock -- "
        "Opening Round reconciliation is not serialized against a concurrent holder"
    )

    lock_txn.rollback()
    lock_conn.close()

    thread.join(timeout=5)
    assert not thread.is_alive(), "bootstrap did not resume after the concurrent advisory lock was released"
    rules = OpeningRoundRuleRepository(bootstrap_db).list_accepted_for_season(season_id)
    assert len(rules) == 10
    bootstrap_db.close()
