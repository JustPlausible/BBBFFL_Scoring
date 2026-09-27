"""PostgreSQL-specific regression for `DatabaseConnection.execute_bounded`
(issue #243 review): its `statement_timeout` is a PostgreSQL session
setting (`SET LOCAL`) that SQLite has no equivalent for, so
`test_db_connection_lifecycle.py` can only prove the SQLite no-op path.
This file proves the real thing -- that `execute_bounded` actually makes
PostgreSQL abort a genuinely slow query at the requested bound, that a
normal (fast) query is unaffected, and that the timeout never leaks onto
whatever query a later caller runs on the same pooled connection.
"""

import os

import pytest
from sqlalchemy.exc import DBAPIError

from app.db import connect
from app.migrations import migrate


@pytest.fixture
def postgres_database():
    base_url = os.getenv("BBBFFL_DATABASE_URL")
    if not base_url or not base_url.startswith("postgresql"):
        pytest.skip("PostgreSQL execute_bounded regression requires BBBFFL_DATABASE_URL")
    migrate(base_url)
    database = connect(base_url)
    yield database
    database.close()


def test_execute_bounded_aborts_a_query_that_exceeds_the_timeout(postgres_database):
    with pytest.raises(DBAPIError, match="statement timeout"):
        postgres_database.execute_bounded("SELECT pg_sleep(1)", timeout_seconds=0.1)


def test_execute_bounded_does_not_affect_a_query_within_the_timeout(postgres_database):
    result = postgres_database.execute_bounded("SELECT 1 AS one", timeout_seconds=5)

    assert result.fetchone()["one"] == 1


def test_execute_bounded_timeout_does_not_leak_onto_a_later_call_on_the_same_pooled_connection(postgres_database):
    """SET LOCAL is transaction-scoped, so it must revert once
    execute_bounded's own connection is released -- a later, unrelated
    caller that happens to be handed the same pooled connection must never
    inherit a prior call's statement_timeout."""
    with pytest.raises(DBAPIError, match="statement timeout"):
        postgres_database.execute_bounded("SELECT pg_sleep(1)", timeout_seconds=0.1)

    # A genuinely slow-but-legitimate query, run with no timeout at all
    # immediately afterward, must not have inherited the prior call's bound.
    result = postgres_database.execute("SELECT pg_sleep(0.3), 1 AS one")
    assert result.fetchone()["one"] == 1
