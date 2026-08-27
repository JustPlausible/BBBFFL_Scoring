"""Production PostgreSQL serialization regressions for player-level locks.

Proves the concurrency invariant issue #34 requires: an edit prepared
against stale lock information must never commit against a player already
locked at the authoritative decision point, and concurrent *observers* of a
lock (e.g. two coach/scorer page loads) can never corrupt or duplicate the
durable lock evidence they race to materialize.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from app.afl_client import Match, Team
from app.db import connect
from app.lineups import LineupConflictError, WeeklyLineupRepository
from app.lockouts import LockedSelectionError, LockoutRepository, LockoutTriggerRepository, LockState, TriggerAlreadyActivatedError
from app.migrations import migrate

EARLY_HOME = Team(5001, "Concurrency FC")
EARLY_AWAY = Team(5002, "Concurrency Opp")
START = datetime(2027, 5, 1, 19, 20, tzinfo=timezone.utc)


def match():
    return Match(match_id=70001, home_team=EARLY_HOME, away_team=EARLY_AWAY, status="UPCOMING", start_time_utc=START.isoformat())


class FixedMatchFacts:
    def matches_for(self, bbbffl_round_id):
        return [match()]


@pytest.fixture(scope="module")
def postgres_url():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    return url


def postgres_context(url, year):
    from app.player_pool import OwnershipRepository, PlayerPoolRepository
    from tests.test_competition_lifecycle import operational

    db = connect(url)
    lifecycle, round_, entries = operational(db, year, year)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    pool, ownership = PlayerPoolRepository(db), OwnershipRepository(db)
    ownership.configure_squad_limit(scope["season_id"], 20)
    incumbent = pool.refresh_player(scope["season_id"], year * 1000, "Incumbent", afl_team_id=EARLY_HOME.team_id, afl_team_name=EARLY_HOME.name)
    ownership.acquire(incumbent.season_player_id, entries[0].season_entry_id)
    challenger = pool.refresh_player(scope["season_id"], year * 1000 + 1, "Challenger", afl_team_id=EARLY_HOME.team_id, afl_team_name=EARLY_HOME.name)
    ownership.acquire(challenger.season_player_id, entries[0].season_entry_id)
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 1, [match().match_id], reason="concurrency fixture")
    return db, round_, entries, scope, incumbent, challenger


def race(commands):
    barrier = Barrier(len(commands))

    def run(command):
        barrier.wait(timeout=5)
        return command()

    with ThreadPoolExecutor(max_workers=len(commands)) as executor:
        return list(executor.map(run, commands))


def test_late_edit_races_a_concurrent_lock_observation_and_fails_safely(postgres_url):
    """One connection submits a legitimately-CAS'd edit that tries to
    change an already-started position; a second, independent connection
    concurrently reads the round's lock state (the same operation a
    coach/scorer page load would trigger) for the same lineup at the same
    moment -- racing to durably materialize the very lock evidence the
    submission depends on. Neither corrupts the other: the submission is
    rejected, the read succeeds, and exactly one lock-evidence row exists
    afterwards with the pre-lock incumbent still the effective selection."""
    db, round_, entries, scope, incumbent, challenger = postgres_context(postgres_url, 2401)
    reader_db = connect(postgres_url)
    lineups = WeeklyLineupRepository(db)
    pre_lock_guard = LockoutRepository(db).guard(match_facts=FixedMatchFacts(), evaluation_at=START - timedelta(minutes=5))
    draft = lineups.save_draft(scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id, {"F1": incumbent.season_player_id}, expected_revision=0)
    submitted = lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0, lock_guard=pre_lock_guard)

    edit = lineups.save_draft(scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id, {"F1": challenger.season_player_id}, expected_revision=draft.revision)
    post_lock_guard = LockoutRepository(db).guard(match_facts=FixedMatchFacts(), evaluation_at=START + timedelta(minutes=1))

    def submit_attempt():
        try:
            lineups.submit(edit.lineup_id, expected_draft_revision=edit.revision, expected_submission_version=submitted.version, lock_guard=post_lock_guard)
            return "committed"
        except LockedSelectionError:
            return "locked"

    def concurrent_read():
        view = LockoutRepository(reader_db).lock_state(
            draft.lineup_id, round_.bbbffl_round_id, entries[0].season_entry_id, {"F1": incumbent.season_player_id},
            match_facts=FixedMatchFacts(), evaluation_at=START + timedelta(minutes=1),
        )
        return view.positions["F1"].state

    submit_result, read_result = race([submit_attempt, concurrent_read])
    assert submit_result == "locked"
    assert read_result == LockState.LOCKED

    effective = lineups.get_effective_submission(draft.lineup_id)
    assert effective.version == submitted.version
    assert effective.positions["F1"] == incumbent.season_player_id
    locks = db.execute("SELECT season_player_id FROM weekly_lineup_lock WHERE lineup_id=? AND position='F1'", (draft.lineup_id,)).fetchall()
    assert [row["season_player_id"] for row in locks] == [incumbent.season_player_id]


def test_concurrent_first_observations_of_a_lock_converge_to_one_record(postgres_url):
    """Two independent connections observe the same never-before-evaluated
    locked position at the same instant (e.g. two coaches' dashboards
    loading simultaneously). PostgreSQL's `ON CONFLICT DO NOTHING` must
    prevent a duplicate-key crash, and both observers must agree on the
    single, durable outcome."""
    db, round_, entries, scope, incumbent, _challenger = postgres_context(postgres_url, 2402)
    reader_a, reader_b = connect(postgres_url), connect(postgres_url)
    lineups = WeeklyLineupRepository(db)
    pre_lock_guard = LockoutRepository(db).guard(match_facts=FixedMatchFacts(), evaluation_at=START - timedelta(minutes=5))
    draft = lineups.save_draft(scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id, {"F1": incumbent.season_player_id}, expected_revision=0)
    submitted = lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0, lock_guard=pre_lock_guard)

    def observe(connection):
        view = LockoutRepository(connection).lock_state(
            draft.lineup_id, round_.bbbffl_round_id, entries[0].season_entry_id, {"F1": incumbent.season_player_id},
            match_facts=FixedMatchFacts(), evaluation_at=START + timedelta(minutes=1),
        )
        return view.positions["F1"]

    first, second = race([lambda: observe(reader_a), lambda: observe(reader_b)])
    assert first.state == second.state == LockState.LOCKED
    assert first.afl_match_id == second.afl_match_id == 70001
    rows = db.execute("SELECT * FROM weekly_lineup_lock WHERE lineup_id=? AND position='F1'", (draft.lineup_id,)).fetchall()
    assert len(rows) == 1


def test_lock_guard_does_not_disturb_ordinary_submission_serialization(postgres_url):
    """With a lock_guard supplied but every relevant match still editable,
    two concurrent submissions racing the same draft revision must behave
    exactly like #33's pre-existing (guard-free) CAS: exactly one wins."""
    db, round_, entries, scope, incumbent, challenger = postgres_context(postgres_url, 2403)
    lineups = WeeklyLineupRepository(db)
    guard = LockoutRepository(db).guard(match_facts=FixedMatchFacts(), evaluation_at=START - timedelta(hours=1))
    draft = lineups.save_draft(scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id, {}, expected_revision=0)

    def attempt(player):
        try:
            edit = lineups.save_draft(scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entries[0].season_entry_id, {"F1": player.season_player_id}, expected_revision=draft.revision)
            lineups.submit(edit.lineup_id, expected_draft_revision=edit.revision, expected_submission_version=0, lock_guard=guard)
            return "committed"
        except LineupConflictError:
            return "conflict"

    results = race([lambda: attempt(incumbent), lambda: attempt(challenger)])
    assert sorted(results) == ["committed", "conflict"]


def test_trigger_replace_races_activation_and_serializes_to_one_safe_outcome(postgres_url):
    """A commissioner reconfiguring a trigger and a coach/scorer read that
    would materialize its activation race for the same trigger's header row
    lock. Exactly one of two safe outcomes must result: either the replace
    committed first (so activation observes the *new* configuration), or
    activation committed first (so the replace is rejected as already-
    activated) -- never a torn state where the persisted match set and the
    persisted activation evidence disagree."""
    db, round_, entries, scope, incumbent, _challenger = postgres_context(postgres_url, 2404)
    triggers = LockoutTriggerRepository(db)
    reader_db = connect(postgres_url)

    def do_replace():
        try:
            triggers.replace(
                round_.bbbffl_round_id, "main", trigger_type="main", sequence=1,
                afl_match_ids=[match().match_id], reason="racing reconfiguration",
            )
            return "replaced"
        except TriggerAlreadyActivatedError:
            return "already_activated"

    def do_materialize():
        LockoutRepository(reader_db)._materialize_round_triggers(
            round_.bbbffl_round_id, match_facts=FixedMatchFacts(), evaluation_at=START + timedelta(minutes=1),
        )
        return "materialized"

    results = race([do_replace, do_materialize])
    assert "materialized" in results

    trigger = triggers.get(round_.bbbffl_round_id, "main")
    activation = db.execute(
        "SELECT revision FROM bbbffl_round_lockout_trigger_activation WHERE trigger_id=?", (trigger.trigger_id,)
    ).fetchone()
    if results[0] == "replaced":
        # The replace committed before activation observed it: activation
        # (if it happened at all) must be recorded against the *new*
        # revision, never a stale one.
        if activation:
            assert activation["revision"] == trigger.revision
    else:
        # Activation won the race: the replace must have been rejected, and
        # the trigger's configuration must remain exactly what it was when
        # it activated.
        assert results[0] == "already_activated"
        assert activation is not None
        assert activation["revision"] == trigger.revision == 1
