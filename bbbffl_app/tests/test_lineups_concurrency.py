"""Production PostgreSQL serialization regressions for weekly lineups."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from app.db import connect
from app.lineups import LineupConflictError, WeeklyLineupRepository
from app.migrations import migrate
from tests.test_lineups import save

@pytest.fixture(scope="module")
def postgres_url():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    return url

def postgres_context(url, year):
    from tests.test_competition_lifecycle import operational
    from app.player_pool import OwnershipRepository, PlayerPoolRepository
    db = connect(url)
    lifecycle, round_, entries = operational(db, year, year)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute("SELECT c.season_id, c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?", (round_.bbbffl_round_id,)).fetchone()
    OwnershipRepository(db).configure_squad_limit(scope["season_id"], 20)
    player = PlayerPoolRepository(db).refresh_player(scope["season_id"], year * 100, "Concurrent Player")
    OwnershipRepository(db).acquire(player.season_player_id, entries[0].season_entry_id)
    return db, round_, entries, scope, player

def race(commands):
    barrier = Barrier(2)
    def run(command):
        barrier.wait(timeout=5)
        try:
            return command()
        except LineupConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(run, commands))

def test_same_draft_revision_has_one_winner(postgres_url):
    db, round_, entries, scope, player = postgres_context(postgres_url, 2201)
    repo = WeeklyLineupRepository(db)
    draft = save(repo, round_, entries, scope, {})
    results = race([lambda: save(repo, round_, entries, scope, {"F1": player.season_player_id}, draft.revision), lambda: save(repo, round_, entries, scope, {"M1": player.season_player_id}, draft.revision)])
    assert sum(result == "conflict" for result in results) == 1

def test_same_submission_version_has_one_winner(postgres_url):
    db, round_, entries, scope, player = postgres_context(postgres_url, 2202)
    repo = WeeklyLineupRepository(db)
    draft = save(repo, round_, entries, scope, {"F1": player.season_player_id})
    results = race([lambda: repo.submit(draft.lineup_id, expected_draft_revision=1, expected_submission_version=0), lambda: repo.submit(draft.lineup_id, expected_draft_revision=1, expected_submission_version=0)])
    assert sum(result == "conflict" for result in results) == 1
    assert repo.get_effective_submission(draft.lineup_id).version == 1
