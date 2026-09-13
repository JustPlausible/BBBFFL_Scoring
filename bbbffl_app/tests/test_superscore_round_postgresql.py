"""PostgreSQL-specific regression for issue #194's fix to
`app.superscore_round._create_review_state_rows`.

That function used to verify the just-inserted review-state row count with
`SELECT COUNT(*) ... FOR UPDATE`. Real PostgreSQL rejects `FOR UPDATE`
combined with an aggregate function ("FOR UPDATE is not allowed with
aggregate functions") -- SQLite silently tolerates it, so
`tests/test_superscore_round.py`'s own (SQLite-backed) coverage of
`setup_round` never caught this. This suite proves `setup_round` -- the
function issue #194's `scripts/superscore_round_2026.py setup-round`
directly depends on -- actually succeeds against real PostgreSQL now,
mirroring `tests/test_finals_seeding_postgresql.py`/`tests/
test_midseason_draft_postgresql.py`'s own PostgreSQL-only precedent."""

import itertools
import os

import pytest

from app.db import connect
from app.migrations import migrate
from app.round_mapping import RoundMappingRepository
from app.superscore_round import EXPECTED_ENTRY_COUNT, ensure_round, ensure_stream, review_state_complete, setup_round
from tests.finals_helpers import KnownRound
from tests.finals_seeding_helpers import build_2026_replay_season

_YEARS = itertools.count(6500)


@pytest.fixture(scope="module")
def postgres_database():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL SuperScore round-setup regression requires BBBFFL_DATABASE_URL")
    migrate(url)
    database = connect(url)
    yield database
    database.close()


def _fresh_stream(database):
    built = build_2026_replay_season(database=database, year=next(_YEARS))
    season = built["season"]
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
    ).fetchone()
    stream = ensure_stream(
        database, season.season_id, rules_row["rules_version_id"], built["competition"].competition_id
    )
    return built, stream


def test_setup_round_succeeds_against_real_postgresql(postgres_database):
    """The regression itself: before issue #194's fix, this raised a
    PostgreSQL `DBAPIError` ("FOR UPDATE is not allowed with aggregate
    functions") instead of completing."""
    built, stream = _fresh_stream(postgres_database)
    season = built["season"]
    round_id = ensure_round(postgres_database, stream.competition_id, 1, 1)
    RoundMappingRepository(postgres_database).accept(round_id, season.year, 9021, KnownRound({(season.year, 9021)}))

    round_row = setup_round(postgres_database, round_id, reason="postgresql regression: SS1 setup")

    assert round_row.bbbffl_round_id == round_id
    assert review_state_complete(postgres_database, round_id)
    count = postgres_database.execute(
        "SELECT COUNT(*) AS n FROM superscore_entry_review_state WHERE bbbffl_round_id=?", (round_id,)
    ).fetchone()["n"]
    assert count == EXPECTED_ENTRY_COUNT


def test_setup_round_is_idempotent_against_real_postgresql(postgres_database):
    built, stream = _fresh_stream(postgres_database)
    season = built["season"]
    round_id = ensure_round(postgres_database, stream.competition_id, 1, 1)
    RoundMappingRepository(postgres_database).accept(round_id, season.year, 9021, KnownRound({(season.year, 9021)}))

    setup_round(postgres_database, round_id, reason="postgresql regression: first setup")
    # A second call must not raise and must not duplicate rows -- the same
    # locked-count check that used to fail outright is also what enforces
    # this idempotency, so this specifically re-exercises the fixed path.
    setup_round(postgres_database, round_id, reason="postgresql regression: repeat setup")

    count = postgres_database.execute(
        "SELECT COUNT(*) AS n FROM superscore_entry_review_state WHERE bbbffl_round_id=?", (round_id,)
    ).fetchone()["n"]
    assert count == EXPECTED_ENTRY_COUNT
