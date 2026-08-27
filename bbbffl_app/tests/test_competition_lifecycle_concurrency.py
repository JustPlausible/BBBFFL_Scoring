"""PostgreSQL regressions for lifecycle and calculation serialization."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.round_mapping as round_mapping_module
from app.db import connect
from app.migrations import migrate
from app.round_mapping import RoundMappingRepository
from tests.test_competition_lifecycle import KnownRound, operational


@pytest.fixture(scope="module")
def postgres_database():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    database = connect(url)
    yield database
    database.close()


def test_open_serializes_against_mapping_correction(postgres_database, monkeypatch):
    lifecycle, round_, _ = operational(postgres_database, 2101, 100)
    correction_holds_mapping_lock = threading.Event()
    allow_correction_to_commit = threading.Event()
    real_append = round_mapping_module.append_event

    def pause_correction_audit(*args, **kwargs):
        # correct() has already locked and advanced round_afl_mapping while its
        # transaction is still uncommitted. Keep that lock long enough to prove
        # upcoming -> open cannot read through it.
        correction_holds_mapping_lock.set()
        assert allow_correction_to_commit.wait(timeout=5)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(round_mapping_module, "append_event", pause_correction_audit)

    with ThreadPoolExecutor(max_workers=2) as executor:
        correction = executor.submit(
            RoundMappingRepository(postgres_database).correct,
            round_.bbbffl_round_id,
            2101,
            101,
            KnownRound(2101, 101),
            reason="concurrent official mapping correction",
        )
        assert correction_holds_mapping_lock.wait(timeout=5)
        opening = executor.submit(lifecycle.transition, round_.bbbffl_round_id, "open")
        time.sleep(0.2)
        assert not opening.done(), "opening did not wait for the mapping row lock"
        allow_correction_to_commit.set()
        correction.result(timeout=5)
        with pytest.raises(ValueError, match="mapping changed"):
            opening.result(timeout=5)

    assert lifecycle.get_round(round_.bbbffl_round_id).state == "upcoming"


def _concurrent_saves(lifecycle, matchup_id):
    ready = threading.Barrier(2)

    def save(value):
        ready.wait(timeout=5)
        return lifecycle.save_calculation(matchup_id, {"value": value})

    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(save, ("a", "b")))


def test_concurrent_calculation_updates_get_distinct_revisions(postgres_database):
    lifecycle, round_, _ = operational(postgres_database, 2102, 100)
    matchup_id = lifecycle.list_matchups(round_.bbbffl_round_id)[0].matchup_id
    assert lifecycle.save_calculation(matchup_id, {"value": "seed"}) == 1

    assert sorted(_concurrent_saves(lifecycle, matchup_id)) == [2, 3]
    persisted = postgres_database.execute(
        "SELECT revision FROM bbbffl_matchup_calculation WHERE matchup_id=?",
        (matchup_id,),
    ).fetchone()
    assert persisted["revision"] == 3


def test_concurrent_first_calculation_creates_revisions_one_and_two(
    postgres_database,
):
    lifecycle, round_, _ = operational(postgres_database, 2103, 100)
    matchup_id = lifecycle.list_matchups(round_.bbbffl_round_id)[0].matchup_id

    assert sorted(_concurrent_saves(lifecycle, matchup_id)) == [1, 2]
    persisted = postgres_database.execute(
        "SELECT revision FROM bbbffl_matchup_calculation WHERE matchup_id=?",
        (matchup_id,),
    ).fetchone()
    assert persisted["revision"] == 2
