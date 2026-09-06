"""Production PostgreSQL serialization regression for issue #146's
missed-submission adjudication: concurrent attempts to create a lineup's
*first* authoritative submission -- whether an ordinary coach submission,
a second adjudication, or the same adjudication retried -- must never
produce two version-1 submissions, and a losing attempt must leave no
partial adjudication/submission trace. Mirrors
`tests/test_lineup_correction_concurrency.py`'s harness."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest

from app.audit import ActorContext
from app.db import connect
from app.lineup_adjudication import LineupAdjudicationService
from app.lineups import EffectiveSubmissionExistsError, LineupConflictError, WeeklyLineupRepository
from app.lockouts import LockedSelectionError, LockoutRepository, LockoutTriggerRepository
from app.migrations import migrate

SCORER = ActorContext.anonymous_operator(role="scorer")
COACH = ActorContext.coach("coach-race")


@pytest.fixture(scope="module")
def postgres_url():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    return url


def _scenario(url, year):
    """A `live` round with one activated selective trigger, a saved
    pre-lockout draft naming an early-match player, and no effective
    submission -- the missed-initial-submission scenario, on a real
    PostgreSQL connection."""
    from app.afl_client import Match, Team
    from app.player_pool import OwnershipRepository, PlayerPoolRepository
    from tests.test_competition_lifecycle import operational

    EARLY_HOME = Team(1001, f"Race Early FC {year}")
    EARLY_AWAY = Team(1002, f"Race Early Opp {year}")
    from datetime import datetime, timezone

    early_start = datetime(2027, 4, 3, 19, 20, tzinfo=timezone.utc)
    early_match_id = year * 10 + 1
    early_match = Match(
        match_id=early_match_id,
        home_team=EARLY_HOME,
        away_team=EARLY_AWAY,
        status="UPCOMING",
        start_time_utc=early_start.isoformat(),
    )

    class FixedMatchFacts:
        def matches_for(self, bbbffl_round_id):
            return [early_match]

    db = connect(url)
    lifecycle, round_, entries = operational(db, year, year)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    lifecycle.transition(round_.bbbffl_round_id, "live")
    scope = db.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r JOIN competition_stream c "
        "ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    entry = entries[0]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(scope["season_id"], 20)
    pool = PlayerPoolRepository(db)
    early_player = pool.refresh_player(
        scope["season_id"], year * 100 + 1, "Race Early Player", afl_team_id=EARLY_HOME.team_id
    )
    ownership.acquire(early_player.season_player_id, entry.season_entry_id)

    triggers = LockoutTriggerRepository(db)
    triggers.create(round_.bbbffl_round_id, "early-1", "selective", 1, [early_match_id], reason="race fixture")
    matches = FixedMatchFacts()
    activation_at = early_start + timedelta(hours=1)
    LockoutRepository(db).materialize_round_triggers(
        round_.bbbffl_round_id, match_facts=matches, evaluation_at=activation_at
    )

    lineups = WeeklyLineupRepository(db)
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": early_player.season_player_id},
        expected_revision=0,
        actor=COACH,
    )
    return db, lifecycle, round_, entry, scope, lineups, matches, draft, early_player, activation_at


def race(commands):
    barrier = Barrier(len(commands))

    def run(command):
        barrier.wait(timeout=5)
        try:
            return command()
        except (LineupConflictError, EffectiveSubmissionExistsError, LockedSelectionError) as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=len(commands)) as executor:
        return list(executor.map(run, commands))


def test_two_concurrent_evidenced_draft_adjudications_never_both_create_version_one(postgres_url):
    db, lifecycle, round_, entry, scope, lineups, matches, draft, early_player, activation_at = _scenario(
        postgres_url, 2901
    )

    def attempt(reason):
        service = LineupAdjudicationService(connect(postgres_url), afl_client=None)
        service.match_facts = matches
        return service.accept_evidenced_draft(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            actor=SCORER,
            reason=reason,
            evaluation_at=activation_at + timedelta(minutes=1),
        )[0]

    results = race([lambda: attempt("race attempt A"), lambda: attempt("race attempt B")])
    failures = [r for r in results if isinstance(r, str)]
    winners = [r for r in results if not isinstance(r, str)]
    assert len(winners) == 1
    assert len(failures) == 1
    assert winners[0].version == 1
    effective = lineups.get_effective_submission(draft.lineup_id)
    assert effective.version == 1
    adjudication_count = db.execute(
        "SELECT COUNT(*) AS n FROM lineup_adjudication WHERE lineup_id=?", (draft.lineup_id,)
    ).fetchone()["n"]
    assert adjudication_count == 1


def test_ordinary_submission_racing_adjudication_never_both_create_version_one(postgres_url):
    """One thread submits ordinarily (via the coach path, its own
    `LockGuard`); the other adjudicates the same missed submission,
    concurrently. `_scenario` deliberately drafts F1 as the already-locked
    player, so the ordinary attempt is always rejected by `LockGuard`
    itself (`LockedSelectionError`) -- exactly issue #146's motivating
    fact that ordinary submission cannot recover this lineup at all -- but
    it must fail *closed*, concurrently, without ever racing the
    adjudication into a corrupted or doubled version-1 state, whichever
    thread the database happens to schedule first."""
    db, lifecycle, round_, entry, scope, lineups, matches, draft, early_player, activation_at = _scenario(
        postgres_url, 2902
    )

    def ordinary_submit():
        conn = connect(postgres_url)
        guard = LockoutRepository(conn).guard(match_facts=matches, evaluation_at=activation_at + timedelta(minutes=1))
        return WeeklyLineupRepository(conn).submit(
            draft.lineup_id,
            expected_draft_revision=draft.revision,
            expected_submission_version=0,
            actor=COACH,
            lock_guard=guard,
        )

    def adjudicate():
        service = LineupAdjudicationService(connect(postgres_url), afl_client=None)
        service.match_facts = matches
        return service.accept_evidenced_draft(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            actor=SCORER,
            reason="racing an ordinary submission",
            evaluation_at=activation_at + timedelta(minutes=1),
        )[0]

    results = race([ordinary_submit, adjudicate])
    failures = [r for r in results if isinstance(r, str)]
    winners = [r for r in results if not isinstance(r, str)]
    assert failures == ["LockedSelectionError"]
    assert len(winners) == 1 and winners[0].version == 1 and winners[0].source_type == "scorer_late_capture"
    effective = lineups.get_effective_submission(draft.lineup_id)
    assert effective.version == 1 and effective.source_type == "scorer_late_capture"
    adjudication_count = db.execute(
        "SELECT COUNT(*) AS n FROM lineup_adjudication WHERE lineup_id=?", (draft.lineup_id,)
    ).fetchone()["n"]
    assert adjudication_count == 1
