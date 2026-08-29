"""Production PostgreSQL serialization regressions for the scorer
round-review workflow (roadmap package 28, issue #58 requirement 7):
`bbbffl_matchup.review_version`'s compare-and-swap only genuinely protects
concurrent scorers against each other when the "read the current version,
then write" pattern is race-free under real row locking -- SQLite has no
`SELECT ... FOR UPDATE`, so these regressions require PostgreSQL, exactly
like tests/test_competition_lifecycle_concurrency.py and
tests/test_lineups_concurrency.py.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.round_review as round_review_module
from app.audit import ActorContext
from app.calculations import MatchupCalculationService
from app.competition_lifecycle import StaleRoundVersionError
from app.db import connect
from app.identity import IdentityRepository
from app.migrations import migrate
from app.round_review import RoundReviewRepository, build_round_review
from tests.round_review_helpers import Facts, full_round, progress_to_review

SCORER = ActorContext.anonymous_operator(role="scorer")
ADMIN = ActorContext.anonymous_operator(role="admin")


@pytest.fixture(scope="module")
def postgres_url():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    return url


def _ready_round(url, year):
    db = connect(url)
    _, lifecycle, round_, entries, stats, canon = full_round(db, year=year)
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    MatchupCalculationService(db, Facts(stats)).calculate_round(round_.bbbffl_round_id)
    return db, lifecycle, round_, entries, canon


def test_concurrent_rulings_on_the_same_slot_serialize_and_the_stale_one_fails(postgres_url):
    db, lifecycle, round_, entries, canon = _ready_round(postgres_url, 9001)
    review_repo = RoundReviewRepository(db)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    ready = threading.Barrier(2)

    def record(dnp):
        ready.wait(timeout=5)
        return review_repo.record_dnp_ruling(
            matchup.matchup_id,
            matchup.home_season_entry_id,
            "F1",
            dnp,
            expected_review_version=1,
            actor=SCORER,
            reason=f"concurrent dnp={dnp}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(record, True)
        second = executor.submit(record, False)
        outcomes, errors = [], []
        for label, future in (("dnp=True", first), ("dnp=False", second)):
            try:
                outcomes.append((label, future.result(timeout=5)))
            except StaleRoundVersionError as exc:
                errors.append(exc)

    assert [version for _, version in outcomes] == [2]
    assert len(errors) == 1
    winning_label = outcomes[0][0]
    persisted = review_repo.get_slot_rulings(matchup.matchup_id)[(matchup.home_season_entry_id, "F1")]
    # Whichever write actually committed decided the outcome, not the
    # loser -- the point of this test is that the loser is rejected
    # (StaleRoundVersionError), never that it silently overwrites the
    # winner or that a particular ordering wins.
    assert persisted.dnp is (winning_label == "dnp=True")
    assert lifecycle.get_matchup(matchup.matchup_id).review_version == 2


def test_concurrent_signoff_attempts_produce_exactly_one_official_version(postgres_url):
    db, lifecycle, round_, entries, canon = _ready_round(postgres_url, 9002)
    review_repo = RoundReviewRepository(db)
    identities = IdentityRepository(db)
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    results = {m.matchup_id: (m.home.effective_score, m.away.effective_score) for m in review.matchups}
    review_versions = {m.matchup_id: m.review_version for m in review.matchups}
    ready = threading.Barrier(2)

    def publish():
        ready.wait(timeout=5)
        return lifecycle.publish_results(
            round_.bbbffl_round_id,
            results,
            actor=SCORER,
            reason="duplicate sign-off race",
            expected_round_version=review.round_version,
            expected_review_versions=review_versions,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish) for _ in range(2)]
        outcomes, errors = [], []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=5))
            except StaleRoundVersionError as exc:
                errors.append(exc)

    assert len(outcomes) == 1
    assert len(errors) == 1
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "final"
    for matchup in lifecycle.list_matchups(round_.bbbffl_round_id):
        history = lifecycle.result_history(matchup.matchup_id)
        assert [h.version for h in history] == [1]
        assert matchup.effective_official_version == 1


def test_stale_correction_attempt_fails_safely_under_true_row_lock_contention(postgres_url, monkeypatch):
    db, lifecycle, round_, entries, canon = _ready_round(postgres_url, 9003)
    review_repo = RoundReviewRepository(db)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]

    review_repo.record_dnp_ruling(
        matchup.matchup_id,
        matchup.home_season_entry_id,
        "F1",
        True,
        expected_review_version=1,
        actor=SCORER,
        reason="initial ruling before publication",
    )
    signoff_review = build_round_review(lifecycle, review_repo, IdentityRepository(db), round_.bbbffl_round_id)
    results = {m.matchup_id: (m.home.effective_score, m.away.effective_score) for m in signoff_review.matchups}
    review_versions = {m.matchup_id: m.review_version for m in signoff_review.matchups}
    lifecycle.publish_results(
        round_.bbbffl_round_id,
        results,
        actor=SCORER,
        reason="initial publication",
        expected_round_version=signoff_review.round_version,
        expected_review_versions=review_versions,
    )
    stale_expected_version = lifecycle.get_matchup(matchup.matchup_id).review_version

    ruling_holds_matchup_lock = threading.Event()
    allow_ruling_to_commit = threading.Event()
    real_append = round_review_module.append_event

    def pause_ruling_audit(*args, **kwargs):
        # record_override has already locked and advanced bbbffl_matchup's
        # review_version while its transaction is still uncommitted. Keep
        # that lock long enough to prove the correction attempt below
        # cannot read a stale review_version through it.
        ruling_holds_matchup_lock.set()
        assert allow_ruling_to_commit.wait(timeout=5)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(round_review_module, "append_event", pause_ruling_audit)

    with ThreadPoolExecutor(max_workers=2) as executor:
        bump = executor.submit(
            review_repo.record_override,
            matchup.matchup_id,
            matchup.home_season_entry_id,
            "F2",
            33.0,
            10.0,
            "post-publication adjustment",
            expected_review_version=stale_expected_version,
            actor=ADMIN,
        )
        assert ruling_holds_matchup_lock.wait(timeout=5)
        correction = executor.submit(
            lifecycle.correct_matchup_result,
            matchup.matchup_id,
            999,
            999,
            reason="stale correction attempt",
            actor=ADMIN,
            expected_review_version=stale_expected_version,
        )
        time.sleep(0.2)
        assert not correction.done(), "correction did not wait for the matchup row lock"
        allow_ruling_to_commit.set()
        bump.result(timeout=5)
        with pytest.raises(StaleRoundVersionError):
            correction.result(timeout=5)

    # The stale correction never committed: version 1 remains effective.
    assert lifecycle.effective_result(matchup.matchup_id).version == 1
