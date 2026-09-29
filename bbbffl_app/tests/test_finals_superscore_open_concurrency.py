"""Issue #214 PostgreSQL regressions: the paired Finals/SuperScore
synchronisation decision (`app.finals_superscore_open._synchronise_locked`)
must serialise safely against every other writer that can touch the same
state -- `app.superscore_round.setup_round` freezing SS's lifecycle
mapping, a direct operator mapping correction
(`app.round_mapping.RoundMappingRepository.correct`), and a lockout-trigger
reconfiguration for either round -- rather than merely narrowing the
windows where those could interleave unsafely.

These are true-concurrency (row-locking) regressions and therefore only
meaningful against real PostgreSQL, exactly like every other
`tests/test_*_concurrency.py` module: SQLite has no row-level locking (see
`app/db.py`'s module docstring), so the deterministic, single-threaded
rollback/no-partial-write regressions live in `tests/test_finals_
superscore_open.py` instead (see
`test_synchronise_lockout_plan_rolls_back_a_mapping_correction_when_a_
trigger_activates_mid_transaction`). This module skips entirely unless
`BBBFFL_DATABASE_URL` points at PostgreSQL."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.competition_lifecycle as competition_lifecycle_module
import app.lockouts as lockouts_module
import app.round_mapping as round_mapping_module
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import connect
from app.finals_preflight import open_finals_week
from app.finals_superscore_open import FrozenMappingDivergedError, synchronise_lockout_plan_from_finals
from app.lockouts import LockoutTriggerRepository
from app.migrations import migrate
from app.round_mapping import AflApiReferenceValidator, RoundMappingRepository
from app.superscore_round import confirm_afl_mapping, setup_round
from tests.finals_helpers import KnownRound
from tests.test_finals_superscore_open import ACTOR, _StubAflClient, _seed


@pytest.fixture(scope="module")
def postgres_database():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    database = connect(url)
    yield database
    database.close()


def test_concurrent_setup_round_freezing_a_stale_mapping_is_caught_not_silently_overwritten(
    postgres_database, monkeypatch
):
    """Issue #214's own representative failure case: "paired synchronisation
    checks SS and sees no frozen mapping; a concurrent `setup_round()`
    freezes the old mapping; synchronisation then corrects the mutable
    head anyway" -- leaving SS's frozen lifecycle mapping pointing at a
    different real AFL round than its mutable head. This proves that
    outcome is now impossible: `setup_round` (via `create_non_ordinary_
    round`) and `_synchronise_locked` both lock SS's own `bbbffl_round` row
    first, so whichever actually runs first fully commits before the other
    can even read the state it decides from. Here `setup_round` wins the
    race and freezes a stale mapping; synchronisation, forced to wait for
    it, must then observe that frozen divergence and fail closed rather
    than silently overwrite only the mutable head."""
    year = 9601
    built = _seed(postgres_database, year)
    afl_round_id = built["afl_round_id"]
    ss_round_id = built["ss1_round_id"]
    open_finals_week(postgres_database, built["bracket"].bracket_id, 1, actor=ACTOR)

    # An earlier, since-superseded admin action gave SS its own accepted
    # mapping onto a *different* AFL round than finals' -- test setup, not
    # the code under test. Synchronisation has not run yet.
    stale_afl_round_id = afl_round_id + 1
    confirm_afl_mapping(
        postgres_database,
        KnownRound({(year, stale_afl_round_id)}),
        ss_round_id,
        year,
        stale_afl_round_id,
        reason="stale pre-existing SS mapping",
    )

    holds_lock = threading.Event()
    allow_commit = threading.Event()
    real_append = competition_lifecycle_module.append_event

    def pause_setup(*args, **kwargs):
        holds_lock.set()
        assert allow_commit.wait(timeout=5)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(competition_lifecycle_module, "append_event", pause_setup)

    with ThreadPoolExecutor(max_workers=2) as executor:
        setup = executor.submit(setup_round, postgres_database, ss_round_id, reason="concurrent setup")
        assert holds_lock.wait(timeout=5)
        sync = executor.submit(
            synchronise_lockout_plan_from_finals,
            postgres_database,
            AflApiReferenceValidator(_StubAflClient(afl_round_id)),
            ss_round_id,
            actor=ACTOR,
        )
        time.sleep(0.2)
        assert not sync.done(), "synchronisation did not wait for setup_round's own row lock"
        allow_commit.set()
        setup.result(timeout=5)
        with pytest.raises(FrozenMappingDivergedError):
            sync.result(timeout=5)

    # Nothing mutated by the losing synchronisation attempt: SS's mutable
    # mapping head still reads the stale value `setup_round` froze, and the
    # frozen lifecycle row agrees with it -- no divergence between the two
    # was ever created, and no silent partial correction happened either.
    ss_mapping = RoundMappingRepository(postgres_database).resolve(ss_round_id)
    assert ss_mapping.afl_round_id == stale_afl_round_id
    frozen = CompetitionLifecycleRepository(postgres_database).get_round(ss_round_id)
    assert frozen.afl_round_id == stale_afl_round_id


def test_concurrent_direct_mapping_correction_serializes_against_paired_synchronisation(
    postgres_database, monkeypatch
):
    """A different operator correcting SS's mapping directly (e.g. via the
    round-preflight UI's mapping-correction form,
    `app.round_mapping.RoundMappingRepository.correct`) while a paired
    synchronisation is also deciding whether SS's mapping needs correcting
    must serialise through the same round-row lock, never interleave: one
    fully completes before the other even reads the mapping it decides
    from, so the final state always reflects exactly one, complete,
    coherent decision -- never a lost update or a torn read."""
    year = 9602
    built = _seed(postgres_database, year)
    afl_round_id = built["afl_round_id"]
    ss_round_id = built["ss1_round_id"]
    open_finals_week(postgres_database, built["bracket"].bracket_id, 1, actor=ACTOR)

    # Give SS an initial, already-accepted mapping matching finals -- so
    # the direct correction below (which requires an existing accepted
    # mapping) is itself legal, ordinary setup.
    confirm_afl_mapping(
        postgres_database,
        KnownRound({(year, afl_round_id)}),
        ss_round_id,
        year,
        afl_round_id,
        reason="initial SS mapping",
    )

    diverted_afl_round_id = afl_round_id + 1
    holds_lock = threading.Event()
    allow_commit = threading.Event()
    real_append = round_mapping_module.append_event

    def pause_correction(*args, **kwargs):
        holds_lock.set()
        assert allow_commit.wait(timeout=5)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(round_mapping_module, "append_event", pause_correction)

    with ThreadPoolExecutor(max_workers=2) as executor:
        correction = executor.submit(
            RoundMappingRepository(postgres_database).correct,
            ss_round_id,
            year,
            diverted_afl_round_id,
            KnownRound({(year, afl_round_id), (year, diverted_afl_round_id)}),
            reason="a different operator's concurrent manual correction",
        )
        assert holds_lock.wait(timeout=5)
        sync = executor.submit(
            synchronise_lockout_plan_from_finals,
            postgres_database,
            AflApiReferenceValidator(_StubAflClient(afl_round_id)),
            ss_round_id,
            actor=ACTOR,
        )
        time.sleep(0.2)
        assert not sync.done(), "synchronisation did not wait for the direct correction's own row lock"
        allow_commit.set()
        correction.result(timeout=5)
        result = sync.result(timeout=5)

    # Synchronisation ran *after* the direct correction committed, saw the
    # diverted mapping, and corrected it back to finals' -- never a lost
    # update, and never a partial/torn intermediate state visible anywhere.
    assert result["mapping_synced"] is True
    ss_mapping = RoundMappingRepository(postgres_database).resolve(ss_round_id)
    assert ss_mapping.afl_round_id == afl_round_id
    history = RoundMappingRepository(postgres_database).history(ss_round_id)
    # initial accept, the concurrent correction, and synchronisation's own
    # correction back -- three real, sequential revisions, not two racing
    # writers colliding into one.
    assert [entry.afl_round_id for entry in history] == [afl_round_id, diverted_afl_round_id, afl_round_id]


def test_concurrent_finals_trigger_mutation_serializes_against_paired_synchronisation(postgres_database, monkeypatch):
    """A scorer revising a *finals* lockout trigger (via
    `app.round_preflight.configure_preflight_trigger`, itself layered over
    `LockoutTriggerRepository.configure`) while a paired synchronisation
    for the same week is already deciding what to copy onto SS must
    serialise through finals' own `bbbffl_round` row lock -- the fixed
    finals-then-SS lock order `_synchronise_locked` uses covers this
    exactly as it already covered a concurrent *SS*-side trigger change
    (issue #211 review, rounds 6-8); this proves the finals side too."""
    year = 9603
    built = _seed(postgres_database, year, with_lockout_triggers=False)
    afl_round_id = built["afl_round_id"]
    ss_round_id = built["ss1_round_id"]
    finals_round_id = built["week1_round_id"]
    trigger_repo = LockoutTriggerRepository(postgres_database)
    trigger_repo.configure(finals_round_id, "main", "main", 1, [9999], actor=ACTOR, reason="main")
    open_finals_week(postgres_database, built["bracket"].bracket_id, 1, actor=ACTOR)

    # An initial, uncontended synchronisation gives SS a matching mapping
    # and an unactivated "main" trigger -- ordinary setup.
    synchronise_lockout_plan_from_finals(
        postgres_database, AflApiReferenceValidator(_StubAflClient(afl_round_id)), ss_round_id, actor=ACTOR
    )

    holds_lock = threading.Event()
    allow_commit = threading.Event()
    real_append = lockouts_module.append_event

    def pause_configure(*args, **kwargs):
        holds_lock.set()
        assert allow_commit.wait(timeout=5)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(lockouts_module, "append_event", pause_configure)

    with ThreadPoolExecutor(max_workers=2) as executor:
        finals_change = executor.submit(
            trigger_repo.configure,
            finals_round_id,
            "main",
            "main",
            1,
            [8888],
            actor=ACTOR,
            reason="concurrent finals main trigger revision",
        )
        assert holds_lock.wait(timeout=5)
        sync = executor.submit(
            synchronise_lockout_plan_from_finals,
            postgres_database,
            AflApiReferenceValidator(_StubAflClient(afl_round_id)),
            ss_round_id,
            actor=ACTOR,
        )
        time.sleep(0.2)
        assert not sync.done(), "synchronisation did not wait for finals' own trigger row lock"
        allow_commit.set()
        finals_change.result(timeout=5)
        result = sync.result(timeout=5)

    # Synchronisation ran *after* the finals trigger change committed, so
    # it copied the *new* match coverage onto SS -- never the stale
    # snapshot it would have read had the two interleaved.
    assert result["synced_trigger_keys"] == ["main"]
    ss_main = trigger_repo.get(ss_round_id, "main")
    assert ss_main.afl_match_ids == (8888,)
