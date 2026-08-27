"""PostgreSQL regressions for lifecycle and calculation serialization."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.round_mapping as round_mapping_module
from app.afl_client import PlayerStatLine
from app.calculations import MatchupCalculationService
from app.db import connect
from app.migrations import migrate
from app.round_mapping import RoundMappingRepository
from tests.test_calculations import Facts, setup_round
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


def test_concurrent_identical_first_service_calculations_settle_at_revision_one(
    postgres_database,
):
    database, lifecycle, round_, stats = setup_round(postgres_database, year=2104)
    matchup_id = lifecycle.list_matchups(round_.bbbffl_round_id)[0].matchup_id
    ready = threading.Barrier(2)

    def calculate():
        ready.wait(timeout=5)
        return MatchupCalculationService(database, Facts(stats)).calculate_matchup(
            matchup_id, upstream_revision="same", observed_at="2104-01-01T00:00:00Z"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: calculate(), range(2)))

    assert [result.revision for result in results] == [1, 1]
    persisted = postgres_database.execute(
        "SELECT revision, input_fingerprint FROM bbbffl_matchup_calculation WHERE matchup_id=?",
        (matchup_id,),
    ).fetchone()
    assert persisted["revision"] == 1
    assert persisted["input_fingerprint"] == results[0].input_fingerprint


def _concurrent_service_calculations(database, matchup_id, stat_sets, observed_at):
    ready = threading.Barrier(2)

    def calculate(stats):
        ready.wait(timeout=5)
        return MatchupCalculationService(database, Facts(stats)).calculate_matchup(matchup_id, observed_at=observed_at)

    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(calculate, stat_sets))


def _changed_stats(stats, goals):
    changed = dict(stats)
    player_id = next(iter(changed))
    changed[player_id] = PlayerStatLine(player_id, goals=goals)
    return changed


def test_concurrent_different_first_service_calculations_advance_atomically(
    postgres_database,
):
    database, lifecycle, round_, stats = setup_round(postgres_database, year=2105)
    matchup_id = lifecycle.list_matchups(round_.bbbffl_round_id)[0].matchup_id
    results = _concurrent_service_calculations(
        database,
        matchup_id,
        (_changed_stats(stats, 40), _changed_stats(stats, 50)),
        "2105-01-01T00:00:00Z",
    )

    assert sorted(result.revision for result in results) == [1, 2]
    last = next(result for result in results if result.revision == 2)
    persisted = postgres_database.execute(
        "SELECT revision, input_fingerprint FROM bbbffl_matchup_calculation WHERE matchup_id=?",
        (matchup_id,),
    ).fetchone()
    assert persisted["revision"] == 2
    assert persisted["input_fingerprint"] == last.input_fingerprint


def test_concurrent_identical_existing_service_calculations_remain_idempotent(
    postgres_database,
):
    database, lifecycle, round_, stats = setup_round(postgres_database, year=2106)
    matchup_id = lifecycle.list_matchups(round_.bbbffl_round_id)[0].matchup_id
    service = MatchupCalculationService(database, Facts(stats))
    assert service.calculate_matchup(matchup_id).revision == 1

    results = _concurrent_service_calculations(database, matchup_id, (dict(stats), dict(stats)), "2106-01-01T00:00:00Z")
    assert [result.revision for result in results] == [1, 1]
    assert len({result.input_fingerprint for result in results}) == 1


def test_concurrent_different_existing_service_calculations_advance_atomically(
    postgres_database,
):
    database, lifecycle, round_, stats = setup_round(postgres_database, year=2107)
    matchup_id = lifecycle.list_matchups(round_.bbbffl_round_id)[0].matchup_id
    assert MatchupCalculationService(database, Facts(stats)).calculate_matchup(matchup_id).revision == 1

    results = _concurrent_service_calculations(
        database,
        matchup_id,
        (_changed_stats(stats, 60), _changed_stats(stats, 70)),
        "2107-01-01T00:00:00Z",
    )
    assert sorted(result.revision for result in results) == [2, 3]
    last = next(result for result in results if result.revision == 3)
    persisted = postgres_database.execute(
        "SELECT revision, input_fingerprint FROM bbbffl_matchup_calculation WHERE matchup_id=?",
        (matchup_id,),
    ).fetchone()
    assert persisted["revision"] == 3
    assert persisted["input_fingerprint"] == last.input_fingerprint
