"""Issue #190: PostgreSQL-only regressions proving genuine row-locking
serialization, not an unlocked re-read -- the acceptance criteria this
issue's Codex review rounds repeatedly strengthened:

- bracket creation's ladder-fallback seed read must be re-verified under a
  deterministic-order `SELECT ... FOR UPDATE` inside the same transaction
  that persists `finals_bracket`, aborting (`StaleSeedOrderError`) if a
  concurrent correction changed a captured result reference;
- `advance_bracket` must lock its prerequisite matchup row(s) the same way,
  aborting (`StaleFinalsResultError`) if a concurrent correction changed
  the result underneath a caller's earlier `preview_advance_bracket` read;
- the finals correction boundary (`CompetitionLifecycleRepository.
  correct_matchup_result`, already finals-compatible) and both of the above
  must serialize against each other via the *same* `bbbffl_matchup` row
  lock, never merely an unlocked second `SELECT`.

Each test below has a thread genuinely hold a database row lock open (by
pausing inside its own transaction, past the point the lock is acquired)
while a second thread's competing transaction demonstrably blocks trying to
acquire the identical row lock -- proven by asserting the second thread's
future is not yet done -- before the first thread's transaction is allowed
to commit and release it. This is the same pattern
tests/test_competition_lifecycle_concurrency.py already established for
issue #152's mapping-correction race."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.competition_lifecycle as competition_lifecycle_module
import app.finals as finals_module
from app.audit import ActorContext
from app.db import connect
from app.finals import (
    DownstreamPlayStateError,
    FinalsBracketAdvanceStateError,
    FinalsBracketRepository,
    StaleFinalsResultError,
    StaleSeedOrderError,
)
from app.finals_preflight import open_finals_week
from app.migrations import migrate
from app.season import SeasonRepository
from tests.finals_helpers import accept_week_mapping, correct_official_result, seed_official_result
from tests.finals_seeding_helpers import build_2026_replay_season

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


def _finals_ready_season(database, year):
    built = build_2026_replay_season(database=database, year=year)
    seasons = SeasonRepository(database)
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (built["season"].season_id,)
    ).fetchone()
    finals_competition = seasons.create_competition(
        built["season"].season_id, rules_row["rules_version_id"], "finals", "Finals", "finals"
    )
    built["finals_competition"] = finals_competition
    built["ordinary_competition_id"] = built["competition"].competition_id
    return built


def test_concurrent_correction_between_ladder_read_and_bracket_commit_is_detected(postgres_database, monkeypatch):
    """A correction to a captured regular-season result, fully committed in
    the gap between bracket creation's ladder read and its own transaction's
    locked re-check, must be detected and abort bracket creation -- never
    silently frozen into `finals_bracket`."""
    built = _finals_ready_season(postgres_database, 2900)
    repo = FinalsBracketRepository(postgres_database)

    real_resolve_seed = FinalsBracketRepository._resolve_seed
    read_done = threading.Event()
    allow_transaction_to_proceed = threading.Event()

    def paused_resolve_seed(self, *args, **kwargs):
        result = real_resolve_seed(self, *args, **kwargs)
        read_done.set()
        assert allow_transaction_to_proceed.wait(timeout=5)
        return result

    monkeypatch.setattr(FinalsBracketRepository, "_resolve_seed", paused_resolve_seed)

    with ThreadPoolExecutor(max_workers=2) as executor:
        creation = executor.submit(
            repo.create_bracket,
            built["season"].season_id,
            built["finals_competition"].competition_id,
            built["ordinary_competition_id"],
            actor=ACTOR,
            reason="race: ladder read then stale commit",
        )
        assert read_done.wait(timeout=5)

        # A fully independent, uncontested correction commits an already-
        # captured Round-20 result's version while bracket creation's read
        # has already happened but its transaction has not yet started.
        matchup_id = postgres_database.execute(
            """
            SELECT m.matchup_id FROM bbbffl_matchup m
            JOIN bbbffl_round_lifecycle l ON l.bbbffl_round_id = m.bbbffl_round_id
            WHERE l.competition_id=? ORDER BY m.matchup_id LIMIT 1
            """,
            (built["ordinary_competition_id"],),
        ).fetchone()["matchup_id"]
        correct_official_result(
            postgres_database, matchup_id, 500, 1, reason="concurrent correction races bracket creation"
        )

        allow_transaction_to_proceed.set()
        with pytest.raises(StaleSeedOrderError):
            creation.result(timeout=5)

    assert repo.get_bracket(built["season"].season_id, built["finals_competition"].competition_id) is None


def test_bracket_creation_transaction_genuinely_blocks_on_the_same_row_a_correction_holds(
    postgres_database, monkeypatch
):
    """Not merely an unlocked re-`SELECT`: bracket creation's own `SELECT
    ... FOR UPDATE` must actually wait for a concurrent correction's row
    lock on the identical matchup to release, then observe the now-changed
    version and abort -- proven by asserting the creation call has not yet
    returned while the correction still holds its lock open."""
    built = _finals_ready_season(postgres_database, 2901)
    repo = FinalsBracketRepository(postgres_database)
    matchup_id = postgres_database.execute(
        """
        SELECT m.matchup_id FROM bbbffl_matchup m
        JOIN bbbffl_round_lifecycle l ON l.bbbffl_round_id = m.bbbffl_round_id
        WHERE l.competition_id=? ORDER BY m.matchup_id LIMIT 1
        """,
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
            correct_official_result, postgres_database, matchup_id, 500, 1, reason="holds the row lock open"
        )
        assert correction_holds_lock.wait(timeout=5)

        creation = executor.submit(
            repo.create_bracket,
            built["season"].season_id,
            built["finals_competition"].competition_id,
            built["ordinary_competition_id"],
            actor=ACTOR,
            reason="must block on the same row lock, not merely re-read",
        )
        time.sleep(0.2)
        assert not creation.done(), "bracket creation did not wait for the matchup row lock"

        allow_correction_to_commit.set()
        correction.result(timeout=5)
        with pytest.raises(StaleSeedOrderError):
            creation.result(timeout=5)

    assert repo.get_bracket(built["season"].season_id, built["finals_competition"].competition_id) is None


def _bracket_at_week1_played(database, year):
    built = _finals_ready_season(database, year)
    repo = FinalsBracketRepository(database)
    bracket = repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="advance-bracket concurrency fixture",
    )["bracket"]
    for week in (1, 2, 3, 4):
        round_id = repo.get_week_round_id(bracket.bracket_id, week)
        accept_week_mapping(database, round_id, year=year, afl_round_id=8900 + week)
    open_finals_week(database, bracket.bracket_id, 1, actor=ACTOR)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(database, pairings["qf"].matchup_id, 100, 50)
    seed_official_result(database, pairings["ef"].matchup_id, 50, 100)
    return built, bracket, repo, pairings


def test_concurrent_correction_racing_an_in_flight_advance_is_detected_via_row_locking(postgres_database, monkeypatch):
    """A correction to the Qualifying Final's result, fully committed after
    `preview_advance_bracket` captured its expected version but genuinely
    serialized against `advance_bracket`'s own row lock on that identical
    matchup, must be detected -- `advance_bracket` aborts for retry rather
    than persisting a pairing derived from the now-superseded QF winner."""
    built, bracket, repo, pairings = _bracket_at_week1_played(postgres_database, 2902)
    preview = repo.preview_advance_bracket(bracket.bracket_id, 1)
    expected_versions = preview["expected_versions"]

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
            pairings["qf"].matchup_id,
            10,
            90,  # flips the QF winner from home (seed2) to away (seed3)
            reason="concurrent QF correction races an in-flight advance",
        )
        assert correction_holds_lock.wait(timeout=5)

        advance = executor.submit(
            repo.advance_bracket,
            bracket.bracket_id,
            1,
            actor=ACTOR,
            reason="advance must block on the same row lock",
            expected_versions=expected_versions,
        )
        time.sleep(0.2)
        assert not advance.done(), "advance_bracket did not wait for the QF matchup row lock"

        allow_correction_to_commit.set()
        correction.result(timeout=5)
        with pytest.raises(StaleFinalsResultError):
            advance.result(timeout=5)

    # No stale Week 2 pairing was persisted -- the advance aborted entirely.
    assert repo.list_pairings(bracket.bracket_id, week_number=2) == ()


def test_rewind_genuinely_blocks_on_the_same_lifecycle_row_a_lineup_submission_holds(postgres_database, monkeypatch):
    """Codex review, PR #201: an unlocked read of `weekly_lineup` inside
    `rewind_bracket`'s downstream-play-state check can miss an authoritative
    submission that commits immediately afterward. `_downstream_play_state`
    now locks the identical `bbbffl_round_lifecycle` row `app.lineups.
    WeeklyLineupRepository._finalize_submission` locks before it commits a
    submission -- proven here by having a submission genuinely hold that
    row lock open while `rewind_bracket`'s own transaction demonstrably
    blocks trying to acquire it, before observing the submission and
    correctly failing closed."""
    import app.lineups as lineups_module
    from app.lineups import WeeklyLineupRepository
    from app.player_pool import OwnershipRepository, PlayerPoolRepository

    built, bracket, repo, pairings = _bracket_at_week1_played(postgres_database, 2904)
    repo.advance_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="advance to week 2")
    open_finals_week(postgres_database, bracket.bracket_id, 2, actor=ACTOR)
    first_semi = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "first_semi")
    round_id = repo.get_week_round_id(bracket.bracket_id, 2)

    scope = postgres_database.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id = r.competition_id WHERE r.bbbffl_round_id=?",
        (round_id,),
    ).fetchone()
    OwnershipRepository(postgres_database).configure_squad_limit(scope["season_id"], 5)
    player = PlayerPoolRepository(postgres_database).refresh_player(scope["season_id"], 970001, "Rewind Race Player")
    OwnershipRepository(postgres_database).acquire(player.season_player_id, first_semi.home_season_entry_id)
    lineups = WeeklyLineupRepository(postgres_database)
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_id,
        first_semi.home_season_entry_id,
        {"F1": player.season_player_id},
        expected_revision=0,
    )

    submission_holds_lock = threading.Event()
    allow_submission_to_commit = threading.Event()
    real_append = lineups_module.append_event

    def pause_submission(*args, **kwargs):
        submission_holds_lock.set()
        assert allow_submission_to_commit.wait(timeout=5)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(lineups_module, "append_event", pause_submission)

    correct_official_result(
        postgres_database, pairings["qf"].matchup_id, 10, 90, reason="concurrent QF correction races a submission"
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        submission = executor.submit(
            lineups.submit, draft.lineup_id, expected_draft_revision=1, expected_submission_version=0
        )
        assert submission_holds_lock.wait(timeout=5)

        rewind = executor.submit(
            repo.rewind_bracket,
            bracket.bracket_id,
            1,
            actor=ACTOR,
            reason="must block on the same lifecycle row lock",
            apply=True,
        )
        time.sleep(0.2)
        assert not rewind.done(), "rewind_bracket did not wait for the round lifecycle row lock"

        allow_submission_to_commit.set()
        submission.result(timeout=5)
        with pytest.raises(DownstreamPlayStateError) as excinfo:
            rewind.result(timeout=5)

    blocked = next(c for c in excinfo.value.report["pairing_changes"] if c["blocked"])
    assert blocked["slot"] == "first_semi"
    assert any(a["type"] == "lineup_submission" for a in blocked["artifacts"])


def test_advance_bracket_locks_both_prerequisite_matchups_in_deterministic_order(postgres_database, monkeypatch):
    """`advance_bracket(from_week=1)` depends on both the QF and EF results;
    both rows must be locked, in deterministic (sorted matchup_id) order, so
    two concurrent advance attempts (or an advance racing a correction on
    either prerequisite) can never deadlock and never partially apply."""
    built, bracket, repo, pairings = _bracket_at_week1_played(postgres_database, 2903)
    matchup_ids = sorted((pairings["qf"].matchup_id, pairings["ef"].matchup_id))

    locked_order = []
    real_lock = FinalsBracketRepository._lock_matchup_version

    def recording_lock(self, conn, matchup_id, expected_versions):
        locked_order.append(matchup_id)
        return real_lock(self, conn, matchup_id, expected_versions)

    monkeypatch.setattr(FinalsBracketRepository, "_lock_matchup_version", recording_lock)
    repo.advance_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="deterministic lock order")

    assert locked_order == matchup_ids


def test_concurrent_advance_bracket_calls_for_the_same_week_fail_closed_not_with_a_raw_integrity_error(
    postgres_database, monkeypatch
):
    """Codex review, PR #201: `advance_bracket`'s target-slot pre-check ran
    before any prerequisite matchup was locked, so two concurrent calls for
    the same week could both pass it; the loser -- having waited on the
    winner's prerequisite matchup lock(s) -- would then try to insert into
    the now-occupied slot and hit the partial unique index as a raw,
    uncaught IntegrityError instead of the intended, retryable domain
    refusal. Proven here by having the winner hold its transaction open
    (via a paused append_event, after its own prerequisite locks and
    pairing inserts but before commit) while the loser's own
    advance_bracket call genuinely blocks trying to acquire the identical
    matchup locks, then resumes only after the winner commits."""
    built, bracket, repo, pairings = _bracket_at_week1_played(postgres_database, 2905)

    winner_holds_lock = threading.Event()
    allow_winner_to_commit = threading.Event()
    real_append = finals_module.append_event
    call_count = {"n": 0}

    def pause_first_advance(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            winner_holds_lock.set()
            assert allow_winner_to_commit.wait(timeout=5)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(finals_module, "append_event", pause_first_advance)

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(repo.advance_bracket, bracket.bracket_id, 1, actor=ACTOR, reason="winner of the race")
        assert winner_holds_lock.wait(timeout=5)

        loser = executor.submit(repo.advance_bracket, bracket.bracket_id, 1, actor=ACTOR, reason="loser of the race")
        time.sleep(0.2)
        assert not loser.done(), "advance_bracket did not wait for the same prerequisite matchup lock(s)"

        allow_winner_to_commit.set()
        winner.result(timeout=5)
        with pytest.raises(FinalsBracketAdvanceStateError):
            loser.result(timeout=5)

    assert len(repo.list_pairings(bracket.bracket_id, week_number=2)) == 2


def test_rewind_reloads_the_pairings_matchup_id_after_a_concurrent_materialisation_races_it(
    postgres_database, monkeypatch
):
    """Codex review, PR #201: rewind_bracket's unlocked `existing` pairing
    read can capture matchup_id=NULL, then wait to acquire the same
    lifecycle lock a concurrent `_materialise_pairing` call already holds
    while attaching a real matchup to that exact pairing. Once that
    materialisation commits and rewind's own lock attempt succeeds, using
    the stale cached matchup_id would leave the just-created matchup
    referenced by neither the superseded nor the replacement pairing, while
    still occupying the round's own uq_round_matchup_order slot. Proven
    here by having the materialisation hold its lifecycle+pairing locks
    open (via a paused append_event) while rewind's own transaction
    genuinely blocks trying to acquire the identical lifecycle lock, then
    resumes only once the materialisation commits.

    Uses an Elimination Final correction (not a Qualifying Final one) so
    only `first_semi` changes -- `second_semi` (seed1 v QF winner) is
    entirely unaffected by the EF result, which keeps this test's outcome
    independent of which of week 2's two slots `open_finals_week`'s own
    loop happens to materialise first."""
    built, bracket, repo, pairings = _bracket_at_week1_played(postgres_database, 2906)
    repo.advance_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="advance to week 2")
    # `_bracket_at_week1_played` already accepted every week's mapping.
    first_semi = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "first_semi")
    assert first_semi.matchup_id is None  # not yet materialised

    # Flips the EF result: only first_semi's away entry (the new EF winner)
    # changes -- second_semi is untouched, so it can never be blocked/
    # superseded here regardless of materialisation order.
    correct_official_result(postgres_database, pairings["ef"].matchup_id, 10, 90, reason="races the materialisation")

    materialisation_holds_lock = threading.Event()
    allow_materialisation_to_commit = threading.Event()
    real_append = finals_module.append_event

    def pause_materialisation(conn, *args, **kwargs):
        if kwargs.get("payload", {}).get("finals_pairing_id") == first_semi.pairing_id:
            materialisation_holds_lock.set()
            assert allow_materialisation_to_commit.wait(timeout=5)
        return real_append(conn, *args, **kwargs)

    monkeypatch.setattr(finals_module, "append_event", pause_materialisation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        materialise = executor.submit(open_finals_week, postgres_database, bracket.bracket_id, 2, actor=ACTOR)
        assert materialisation_holds_lock.wait(timeout=5)

        rewind = executor.submit(
            repo.rewind_bracket, bracket.bracket_id, 1, actor=ACTOR, reason="races the materialisation", apply=True
        )
        time.sleep(0.2)
        assert not rewind.done(), (
            "rewind_bracket did not wait for the same lifecycle row lock the materialisation holds"
        )

        allow_materialisation_to_commit.set()
        materialise.result(timeout=5)
        rewind_result = rewind.result(timeout=5)

    assert not rewind_result["blocked"]
    round_id = repo.get_week_round_id(bracket.bracket_id, 2)
    matchup_count = postgres_database.execute(
        "SELECT COUNT(*) AS n FROM bbbffl_matchup WHERE bbbffl_round_id=?", (round_id,)
    ).fetchone()["n"]
    assert matchup_count == 2  # first_semi's + second_semi's -- nothing orphaned

    new_first_semi = next(p for p in repo.list_pairings(bracket.bracket_id, week_number=2) if p.slot == "first_semi")
    assert new_first_semi.matchup_id is not None, (
        "the concurrently materialised matchup must have been reconciled onto the replacement pairing, "
        "not left orphaned with the replacement still unmaterialised"
    )
    reused_matchup = postgres_database.execute(
        "SELECT home_season_entry_id, away_season_entry_id FROM bbbffl_matchup WHERE matchup_id=?",
        (new_first_semi.matchup_id,),
    ).fetchone()
    assert reused_matchup["home_season_entry_id"] == new_first_semi.home_season_entry_id
    assert reused_matchup["away_season_entry_id"] == new_first_semi.away_season_entry_id
