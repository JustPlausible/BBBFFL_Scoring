"""Issue #195 acceptance coverage: the shared completed-season write fence
(`app.season.SeasonRepository.guard_writable`, raising `SeasonCompletedError`)
wired into every result-changing repository boundary. Every test here calls
the domain/repository function directly -- never through an HTTP route --
so a pass here proves the refusal happens at the transaction boundary the
issue requires, not merely at route level. Reads are proven unaffected
alongside each refusal."""

from contextlib import contextmanager
from types import SimpleNamespace

from app.audit import ActorContext
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.finals import FinalsBracketRepository
from app.finals_review import correct_finals_result
from app.identity import IdentityRepository
from app.round_review import RoundReviewRepository
from app.season import SeasonCompletedError
from app.season_awards import PREMIERSHIP, SeasonAwardRepository, reconcile_premiership, reconcile_wooden_spoon
from app.season_completion import complete_season
from app.superscore_results import SuperScoreLeaderboardService
from tests.season_completion_helpers import _Facts, build_completable_season

ACTOR = ActorContext.anonymous_operator("test")


def _complete(year):
    built = build_completable_season(year=year)
    result = complete_season(built["database"], built["season"].season_id, actor=ACTOR, reason="complete 2026 replay")
    return built, result


# -- Ordinary result correction -----------------------------------------------


def test_ordinary_matchup_correction_refused_after_completion():
    built, _ = _complete(5200)
    database = built["database"]
    matchup_id = database.execute(
        "SELECT m.matchup_id FROM bbbffl_matchup m JOIN bbbffl_round_lifecycle l ON l.bbbffl_round_id=m.bbbffl_round_id "
        "WHERE l.competition_id=? ORDER BY m.matchup_id LIMIT 1",
        (built["ordinary_competition_id"],),
    ).fetchone()["matchup_id"]
    before = database.execute(
        "SELECT COUNT(*) AS n FROM bbbffl_official_result WHERE matchup_id=?", (matchup_id,)
    ).fetchone()["n"]

    try:
        CompetitionLifecycleRepository(database).correct_matchup_result(
            matchup_id, 999, 1, reason="attempted correction after completion", actor=ACTOR
        )
        raise AssertionError("expected SeasonCompletedError")
    except SeasonCompletedError:
        pass

    after = database.execute(
        "SELECT COUNT(*) AS n FROM bbbffl_official_result WHERE matchup_id=?", (matchup_id,)
    ).fetchone()["n"]
    assert after == before  # nothing was written

    # Reads still work.
    effective = CompetitionLifecycleRepository(database).effective_result(matchup_id)
    assert effective is not None


def test_ordinary_round_wide_correction_refused_after_completion():
    built, _ = _complete(5201)
    database = built["database"]
    round_id = database.execute(
        "SELECT bbbffl_round_id FROM bbbffl_round_lifecycle WHERE competition_id=? AND fixture_round_number=20",
        (built["ordinary_competition_id"],),
    ).fetchone()["bbbffl_round_id"]
    matchups = CompetitionLifecycleRepository(database).list_matchups(round_id)
    results = {m.matchup_id: (7, 3) for m in matchups}

    try:
        CompetitionLifecycleRepository(database).correct_results(
            round_id, results, reason="attempted round-wide correction after completion", actor=ACTOR
        )
        raise AssertionError("expected SeasonCompletedError")
    except SeasonCompletedError:
        pass

    round_row = CompetitionLifecycleRepository(database).get_round(round_id)
    assert round_row.state == "final"  # unchanged


# -- Finals result correction, and its pairing/elimination cascade ----------


def test_finals_grand_final_correction_refused_after_completion(monkeypatch):
    """`correct_finals_result` recomputes scores from real AFL evidence
    before it ever opens its write transaction -- the completable-season
    fixture seeds its Grand Final result directly (`seed_official_result`,
    the same shortcut `tests/test_finals.py` itself uses) rather than
    through a full lineup/evidence pipeline, so recomputation itself has
    nothing real to work from. Mocking exactly that recompute/review layer
    (mirroring `tests/test_finals_review.py::
    test_correction_retries_the_whole_transaction_after_stale_materialisation`'s
    identical approach) isolates the one thing this test actually proves:
    the *real*, unmocked `_correct_finals_result_transaction` refuses
    before it writes anything, because the season is completed."""
    built, _ = _complete(5210)
    database = built["database"]
    gf_matchup_id = built["grand_final_matchup_id"]
    before_version = database.execute(
        "SELECT effective_official_version FROM bbbffl_matchup WHERE matchup_id=?", (gf_matchup_id,)
    ).fetchone()["effective_official_version"]

    import app.finals_review as finals_review_module

    fake_review = SimpleNamespace(
        blockers=[],
        review_version=0,
        home=SimpleNamespace(season_entry_id="fake-home", effective_score=1),
        away=SimpleNamespace(season_entry_id="fake-away", effective_score=2),
    )
    monkeypatch.setattr(finals_review_module.MatchupCalculationService, "calculate_round", lambda *_a, **_k: None)
    monkeypatch.setattr(finals_review_module, "build_matchup_review", lambda *_a, **_k: fake_review)
    monkeypatch.setattr(finals_review_module, "_freeze_matchup_inputs", lambda *_a, **_k: {})

    class _NoopEvidence:
        def is_evidence_fresh(self):
            return True

    class _NoopAflClient:
        def evidence_batch(self):
            @contextmanager
            def _scope():
                yield _NoopEvidence()

            return _scope()

    lifecycle = CompetitionLifecycleRepository(database)
    try:
        correct_finals_result(
            database,
            _NoopAflClient(),
            lifecycle,
            RoundReviewRepository(database),
            IdentityRepository(database),
            gf_matchup_id,
            actor=ACTOR,
            reason="attempted GF correction after completion",
        )
        raise AssertionError("expected SeasonCompletedError")
    except SeasonCompletedError:
        pass

    after_version = database.execute(
        "SELECT effective_official_version FROM bbbffl_matchup WHERE matchup_id=?", (gf_matchup_id,)
    ).fetchone()["effective_official_version"]
    assert after_version == before_version

    # Reads still work.
    assert FinalsBracketRepository(database).get_bracket_by_id(built["bracket"].bracket_id) is not None


def test_finals_bracket_rewind_cascade_refused_after_completion():
    """`rewind_bracket` is the standalone correction-triggered pairing/
    elimination cascade path (Steve's confirmed rewind policy) -- distinct
    from `correct_finals_result`'s own nested cascade, and separately
    wired to the same fence."""
    built, _ = _complete(5211)
    database = built["database"]
    repo = FinalsBracketRepository(database)

    try:
        repo.rewind_bracket(
            built["bracket"].bracket_id, 3, actor=ACTOR, reason="attempted rewind after completion", apply=True
        )
        raise AssertionError("expected SeasonCompletedError")
    except SeasonCompletedError:
        pass

    # A read-only preview must still work against a completed season (the
    # fence is a write-path guard only, and `apply=False` never calls it).
    preview = repo.rewind_bracket(built["bracket"].bracket_id, 3, actor=ACTOR, reason="preview", apply=False)
    assert preview is not None


# -- SuperScore correction/republication --------------------------------------


def test_superscore_republication_refused_after_completion():
    """`SuperScoreLeaderboardService.publish` recomputes from real AFL
    evidence before it ever opens its write transaction -- reusing the
    identical stat lines the completable-season fixture originally
    published SS1 from (rather than empty evidence) is what lets
    recomputation succeed cleanly, isolating the one thing this test
    actually proves: the *real* `_persist` refuses before it writes
    anything, because the season is completed."""
    built, _ = _complete(5220)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    before = database.execute(
        "SELECT MAX(version) AS v FROM superscore_leaderboard_revision WHERE bbbffl_round_id=?", (round_id,)
    ).fetchone()["v"]

    service = SuperScoreLeaderboardService(database, _Facts(built["superscore_stats"][1]))
    try:
        service.publish(round_id, actor=ACTOR, reason="attempted republication after completion")
        raise AssertionError("expected SeasonCompletedError")
    except SeasonCompletedError:
        pass

    after = database.execute(
        "SELECT MAX(version) AS v FROM superscore_leaderboard_revision WHERE bbbffl_round_id=?", (round_id,)
    ).fetchone()["v"]
    assert after == before

    # Reads still work.
    assert service.leaderboard(round_id) is not None


# -- Premiership/wooden-spoon re-recording ------------------------------------


def test_premiership_and_wooden_spoon_reconciliation_refused_after_completion():
    built, result = _complete(5230)
    database, season_id = built["database"], built["season"].season_id

    try:
        reconcile_premiership(database, season_id, actor=ACTOR, reason="attempted re-recording after completion")
        raise AssertionError("expected SeasonCompletedError")
    except SeasonCompletedError:
        pass
    try:
        reconcile_wooden_spoon(database, season_id, actor=ACTOR, reason="attempted re-recording after completion")
        raise AssertionError("expected SeasonCompletedError")
    except SeasonCompletedError:
        pass

    # Nothing changed: the completion's own award records are still active
    # and unsuperseded.
    assert (
        SeasonAwardRepository(database).get_active(season_id, PREMIERSHIP).award_id == result.premiership_award.award_id
    )


# -- Every refusal is domain-level, not route-only ---------------------------


def test_refusal_is_raised_by_the_shared_guard_type_everywhere():
    """Every path above raises the identical shared `SeasonCompletedError`
    type (`app.season.SeasonRepository.guard_writable`) -- never a bespoke,
    per-module error -- proving one shared guard expresses the contract
    across ordinary, finals and SuperScore paths alike."""
    from app.superscore_results import CompletedSeasonError

    assert CompletedSeasonError is SeasonCompletedError
