"""Regression coverage for the database connection-lifecycle fix in PR #23
review: a DecisionsRepository operation must acquire a pooled connection for
its own duration only, never hold one open across calls or share one across
concurrent requests. See app/db.py.
"""

import threading

from app.db import DecisionsRepository, connect
from app.migrations import migrate


def _migrated_database(tmp_path, name="db.db"):
    url = f"sqlite:///{tmp_path / name}"
    migrate(url)
    return connect(url)


def test_repository_operations_do_not_hold_a_connection_open(tmp_path):
    database = _migrated_database(tmp_path)
    repo = DecisionsRepository(database)

    repo.set_dnp("team_a", "Forward1", True)
    repo.get_dnp_map()

    assert database.engine.pool.checkedout() == 0


def test_read_only_polling_leaves_no_open_transaction(tmp_path):
    """A read-only caller repeatedly polling get_matchup_state() must never
    accumulate an open implicit transaction on a shared connection."""
    database = _migrated_database(tmp_path)
    repo = DecisionsRepository(database)

    for _ in range(5):
        repo.get_matchup_state()

    assert database.engine.pool.checkedout() == 0


def test_concurrent_requests_do_not_share_a_connection_or_transaction(tmp_path):
    """Simulates concurrent synchronous FastAPI handlers (Starlette's thread
    pool) each performing a write through the same repository/engine. Each
    write must land in its own transaction rather than one request
    committing or rolling back another's in-flight work."""
    database = _migrated_database(tmp_path)
    repo = DecisionsRepository(database)
    errors = []

    def worker(i):
        try:
            repo.set_dnp(f"team_{i}", "Forward1", True)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    dnp_map = repo.get_dnp_map()
    assert len(dnp_map) == 10
    assert all(dnp_map[(f"team_{i}", "Forward1")] is True for i in range(10))
    assert database.engine.pool.checkedout() == 0
