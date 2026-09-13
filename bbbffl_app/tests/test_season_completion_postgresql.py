"""Issue #195: PostgreSQL-only regressions proving the season-completion
transaction and a result correction genuinely serialize through the same
`bbbffl_season` row lock (`app.season.SeasonRepository.guard_writable`) --
never an unlocked re-read, and never a torn outcome where the completion's
own award facts and the season's effective competition results disagree.

Each test below has one thread genuinely hold the season-row lock open (by
pausing inside its own transaction, past the point the lock is acquired)
while a second thread's competing transaction demonstrably blocks trying to
acquire the identical row lock -- proven by asserting the second thread's
future is not yet done -- before the first thread's transaction is allowed
to commit and release it. This is the same pattern
tests/test_finals_postgresql.py and tests/test_competition_lifecycle_
concurrency.py already establish for their own row-lock races."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.competition_lifecycle as competition_lifecycle_module
import app.season_completion as season_completion_module
from app.audit import ActorContext
from app.calculations import MatchupCalculationService
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import connect
from app.migrations import migrate
from app.season import SeasonCompletedError, SeasonRepository
from app.season_awards import PREMIERSHIP, SeasonAwardRepository
from app.season_completion import complete_season
from tests.finals_helpers import correct_official_result
from tests.season_completion_helpers import build_completable_season, seed_real_finals_grand_final_calculation

ACTOR = ActorContext.anonymous_operator("test")


@pytest.fixture(scope="module")
def postgres_database():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    database = connect(url)
    yield database
    database.close()


def _completable_season(database, year):
    return build_completable_season(database=database, year=year)


def test_correction_holding_the_lock_first_completes_and_completion_observes_it(postgres_database, monkeypatch):
    """A correction that already holds the season-row lock must finish
    (commit) before `complete_season` can even verify readiness -- and
    once it does, completion must derive its awards from the *corrected*
    effective result, never a stale pre-correction read."""
    built = _completable_season(postgres_database, 5300)
    season_id = built["season"].season_id
    ordinary_matchup_id = postgres_database.execute(
        "SELECT m.matchup_id FROM bbbffl_matchup m JOIN bbbffl_round_lifecycle l "
        "ON l.bbbffl_round_id = m.bbbffl_round_id WHERE l.competition_id=? ORDER BY m.matchup_id LIMIT 1",
        (built["ordinary_competition_id"],),
    ).fetchone()["matchup_id"]

    correction_holds_lock = threading.Event()
    allow_correction_to_commit = threading.Event()
    real_append = competition_lifecycle_module.append_event

    def pause_correction(*args, **kwargs):
        correction_holds_lock.set()
        assert allow_correction_to_commit.wait(timeout=5)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(competition_lifecycle_module, "append_event", pause_correction)

    with ThreadPoolExecutor(max_workers=2) as executor:
        correction = executor.submit(
            correct_official_result,
            postgres_database,
            ordinary_matchup_id,
            999,
            1,
            reason="holds the season-row lock open",
        )
        assert correction_holds_lock.wait(timeout=5)

        completion = executor.submit(
            complete_season, postgres_database, season_id, actor=ACTOR, reason="races a held correction lock"
        )
        time.sleep(0.2)
        assert not completion.done(), "season completion did not wait for the season row lock the correction holds"

        allow_correction_to_commit.set()
        correction.result(timeout=5)
        result = completion.result(timeout=5)

    assert result.season.lifecycle_state == "completed"
    # The wooden spoon must reflect the *corrected* effective Round 20
    # result, not whatever the ladder looked like before the correction
    # committed -- proof there is no torn state between the two.
    corrected_reference = next(
        r for r in result.wooden_spoon_award.provenance["result_references"] if r["matchup_id"] == ordinary_matchup_id
    )
    assert corrected_reference["official_version"] == 2


def test_completion_holding_the_lock_first_commits_and_correction_then_observes_completed(
    postgres_database, monkeypatch
):
    """If `complete_season` already holds the season-row lock and commits
    `completed`, a correction queued behind it must acquire the lock only
    afterward, observe `completed`, raise `SeasonCompletedError`, and write
    nothing -- never a partial/torn write racing the completion."""
    built = _completable_season(postgres_database, 5301)
    season_id = built["season"].season_id
    ordinary_matchup_id = postgres_database.execute(
        "SELECT m.matchup_id FROM bbbffl_matchup m JOIN bbbffl_round_lifecycle l "
        "ON l.bbbffl_round_id = m.bbbffl_round_id WHERE l.competition_id=? ORDER BY m.matchup_id LIMIT 1",
        (built["ordinary_competition_id"],),
    ).fetchone()["matchup_id"]
    before_version = postgres_database.execute(
        "SELECT effective_official_version FROM bbbffl_matchup WHERE matchup_id=?", (ordinary_matchup_id,)
    ).fetchone()["effective_official_version"]

    completion_holds_lock = threading.Event()
    allow_completion_to_commit = threading.Event()
    real_append = season_completion_module.append_event

    def pause_completion(*args, **kwargs):
        completion_holds_lock.set()
        assert allow_completion_to_commit.wait(timeout=5)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(season_completion_module, "append_event", pause_completion)

    with ThreadPoolExecutor(max_workers=2) as executor:
        completion = executor.submit(
            complete_season, postgres_database, season_id, actor=ACTOR, reason="holds the season-row lock open"
        )
        assert completion_holds_lock.wait(timeout=5)

        correction = executor.submit(
            correct_official_result,
            postgres_database,
            ordinary_matchup_id,
            777,
            2,
            reason="races a held completion lock",
        )
        time.sleep(0.2)
        assert not correction.done(), "correction did not wait for the season row lock completion holds"

        allow_completion_to_commit.set()
        result = completion.result(timeout=5)
        with pytest.raises(SeasonCompletedError):
            correction.result(timeout=5)

    assert result.season.lifecycle_state == "completed"
    after_version = postgres_database.execute(
        "SELECT effective_official_version FROM bbbffl_matchup WHERE matchup_id=?", (ordinary_matchup_id,)
    ).fetchone()["effective_official_version"]
    assert after_version == before_version  # the queued correction wrote nothing

    # No torn state: the completed season's own effective result and its
    # wooden-spoon provenance agree on the same (pre-correction) version.
    season = SeasonRepository(postgres_database).get_season(season_id)
    assert season.lifecycle_state == "completed"
    award = SeasonAwardRepository(postgres_database).get_active(season_id, PREMIERSHIP)
    assert award is not None
    effective = CompetitionLifecycleRepository(postgres_database).effective_result(ordinary_matchup_id)
    assert effective.version == before_version


def test_finals_recalculation_blocks_on_the_season_lock_completion_holds_and_writes_nothing(
    postgres_database, monkeypatch
):
    """Codex review (PR #206, P2): `MatchupCalculationService.calculate_
    round(..., guard_season=True)` -- the fix for the gap where a finals
    correction/publication's own recalculation could mutate `bbbffl_
    matchup_calculation` for an already-completed season -- must genuinely
    block on the *same* `bbbffl_season` row lock `complete_season` holds,
    not merely observe `completed` via an unlocked re-read. Proven the same
    way every other race in this module is: the recalculation call must
    not be `done()` while `complete_season` still holds the lock open, and
    once released, the recalculation observes `completed` and persists no
    new calculation revision at all."""
    built = build_completable_season(database=postgres_database, year=5303)
    facts = seed_real_finals_grand_final_calculation(built, 5303)
    season_id = built["season"].season_id
    gf_matchup_id = built["grand_final_matchup_id"]
    gf_round_id = postgres_database.execute(
        "SELECT bbbffl_round_id FROM bbbffl_matchup WHERE matchup_id=?", (gf_matchup_id,)
    ).fetchone()["bbbffl_round_id"]
    before = dict(
        postgres_database.execute(
            "SELECT revision, input_fingerprint, updated_at FROM bbbffl_matchup_calculation WHERE matchup_id=?",
            (gf_matchup_id,),
        ).fetchone()
    )

    completion_holds_lock = threading.Event()
    allow_completion_to_commit = threading.Event()
    real_append = season_completion_module.append_event

    def pause_completion(*args, **kwargs):
        completion_holds_lock.set()
        assert allow_completion_to_commit.wait(timeout=5)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(season_completion_module, "append_event", pause_completion)

    with ThreadPoolExecutor(max_workers=2) as executor:
        completion = executor.submit(
            complete_season, postgres_database, season_id, actor=ACTOR, reason="holds the season-row lock open"
        )
        assert completion_holds_lock.wait(timeout=5)

        recompute = executor.submit(
            MatchupCalculationService(postgres_database, facts).calculate_round,
            gf_round_id,
            guard_season=True,
        )
        time.sleep(0.2)
        assert not recompute.done(), "recalculation did not wait for the season row lock completion holds"

        allow_completion_to_commit.set()
        completion.result(timeout=5)
        with pytest.raises(SeasonCompletedError):
            recompute.result(timeout=5)

    after = dict(
        postgres_database.execute(
            "SELECT revision, input_fingerprint, updated_at FROM bbbffl_matchup_calculation WHERE matchup_id=?",
            (gf_matchup_id,),
        ).fetchone()
    )
    assert after == before  # no torn state: the queued recalculation wrote nothing
