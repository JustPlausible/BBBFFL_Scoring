"""Production PostgreSQL serialization regression for issue #137's
locked-lineup correction (acceptance #18): two competing corrections built
from the same expected submission version must serialize -- exactly one
commits a new version, the other fails closed with `LineupConflictError`,
never silently overwriting or double-applying."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.audit import ActorContext
from app.db import connect
from app.lineups import LineupConflictError, WeeklyLineupRepository
from app.migrations import migrate

SCORER = ActorContext.anonymous_operator(role="scorer")


@pytest.fixture(scope="module")
def postgres_url():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    return url


def _context(url, year):
    from app.player_pool import OwnershipRepository, PlayerPoolRepository
    from tests.test_competition_lifecycle import operational

    db = connect(url)
    lifecycle, round_, entries = operational(db, year, year)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    entry = entries[0]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(scope["season_id"], 20)
    pool = PlayerPoolRepository(db)
    starter = pool.refresh_player(scope["season_id"], year * 100 + 1, "Starter Player")
    ownership.acquire(starter.season_player_id, entry.season_entry_id)
    candidate_a = pool.refresh_player(scope["season_id"], year * 100 + 2, "Correction Candidate A")
    ownership.acquire(candidate_a.season_player_id, entry.season_entry_id)
    candidate_b = pool.refresh_player(scope["season_id"], year * 100 + 3, "Correction Candidate B")
    ownership.acquire(candidate_b.season_player_id, entry.season_entry_id)

    lineups = WeeklyLineupRepository(db)
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": starter.season_player_id},
        expected_revision=0,
    )
    submitted = lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)
    return db, lineups, submitted, candidate_a, candidate_b


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


def test_two_competing_corrections_serialize_and_one_stale_operation_fails(postgres_url):
    db, lineups, submitted, candidate_a, candidate_b = _context(postgres_url, 2801)
    results = race(
        [
            lambda: lineups.submit_correction(
                submitted.lineup_id,
                {**submitted.positions, "M1": candidate_a.season_player_id},
                expected_submission_version=submitted.version,
                actor=SCORER,
                reason="race attempt A: correct M1 to candidate A",
            ),
            lambda: lineups.submit_correction(
                submitted.lineup_id,
                {**submitted.positions, "M1": candidate_b.season_player_id},
                expected_submission_version=submitted.version,
                actor=SCORER,
                reason="race attempt B: correct M1 to candidate B",
            ),
        ]
    )
    assert sum(result == "conflict" for result in results) == 1
    winner = next(result for result in results if result != "conflict")
    assert winner.to_version == submitted.version + 1
    effective = lineups.get_effective_submission(submitted.lineup_id)
    assert effective.version == winner.to_version
    winning_m1_slot = next(slot for slot in winner.slots if slot.position == "M1")
    assert effective.positions["M1"] == winning_m1_slot.corrected_season_player_id
    # Exactly one correction record was ever persisted -- the loser's
    # attempt left no partial trace.
    assert len(lineups.list_corrections(submitted.lineup_id)) == 1
