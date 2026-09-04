"""Production PostgreSQL serialization for Opening Round nominations: two
concurrent nominations that would violate a slot/player/rule uniqueness
invariant (migrations/versions/0020_opening_round_deferral.py) must have
exactly one winner, never both silently persisted -- mirroring
tests/test_carry_forward_concurrency.py's pattern for the same
`FOR UPDATE`-guarded read/insert shape."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.afl_client import Match, Team
from app.audit import ActorContext
from app.db import connect
from app.migrations import migrate
from app.opening_round import (
    OpeningRoundError,
    OpeningRoundNominationRepository,
    OpeningRoundRuleRepository,
    OpeningRoundSubmissionRepository,
)
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests.test_competition_lifecycle import operational
from tests.test_opening_round import KnownRounds, MultiRoundMatchClient


@pytest.fixture(scope="module")
def postgres_url():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    return url


def race(commands):
    barrier = Barrier(len(commands))

    def run(command):
        barrier.wait(timeout=5)
        try:
            return command()
        except OpeningRoundError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=len(commands)) as executor:
        return list(executor.map(run, commands))


def _context(url, year):
    db = connect(url)
    lifecycle, round_, entries = operational(db, year, year)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute(
        "SELECT c.season_id,c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    OwnershipRepository(db).configure_squad_limit(scope["season_id"], 30)
    validator = KnownRounds((year, year), (year, year + 1))
    rule = OpeningRoundRuleRepository(db).accept(
        scope["season_id"],
        2,
        year,
        year,
        year + 1,
        round_.bbbffl_round_id,
        validator,
        actor=ActorContext.anonymous_operator("admin"),
        reason="concurrency test rule",
    )
    entry = entries[0]
    player_a = PlayerPoolRepository(db).refresh_player(scope["season_id"], year * 1000 + 1, "Player A", afl_team_id=2)
    player_b = PlayerPoolRepository(db).refresh_player(scope["season_id"], year * 1000 + 2, "Player B", afl_team_id=2)
    OwnershipRepository(db).acquire(player_a.season_player_id, entry.season_entry_id)
    OwnershipRepository(db).acquire(player_b.season_player_id, entry.season_entry_id)
    client = MultiRoundMatchClient(
        {year: [Match(match_id=year * 10, home_team=Team(2, "BL"), away_team=Team(5, "CARL"), status="CONCLUDED")]}
    )
    return db, rule, entry, player_a, player_b, client


def test_two_concurrent_nominations_for_the_same_slot_have_one_winner(postgres_url):
    db, rule, entry, player_a, player_b, client = _context(postgres_url, 2401)
    nominations = OpeningRoundNominationRepository(db)

    def nominate(player):
        return nominations.nominate(
            rule.rule_id,
            entry.season_entry_id,
            "M1",
            player.season_player_id,
            client,
            actor=ActorContext.anonymous_operator("scorer"),
            reason="concurrent attempt",
        )

    results = race([lambda: nominate(player_a), lambda: nominate(player_b)])
    assert sum(result == "conflict" for result in results) == 1
    winner = next(result for result in results if result != "conflict")
    stored = nominations.get(winner.nomination_id)
    assert stored.season_player_id == winner.season_player_id


def test_two_concurrent_nominations_under_the_same_rule_entry_have_one_winner(postgres_url):
    db, rule, entry, player_a, player_b, client = _context(postgres_url, 2402)
    nominations = OpeningRoundNominationRepository(db)

    def nominate(player, position):
        return nominations.nominate(
            rule.rule_id,
            entry.season_entry_id,
            position,
            player.season_player_id,
            client,
            actor=ActorContext.anonymous_operator("scorer"),
            reason="concurrent attempt",
        )

    results = race([lambda: nominate(player_a, "M1"), lambda: nominate(player_b, "M2")])
    assert sum(result == "conflict" for result in results) == 1


def test_confirm_and_nominate_race_never_leaves_a_confirmed_submission_silently_mutated(postgres_url):
    """Issue #133 PR review (P1): a nomination write and a confirmation for
    the same entry must serialize -- never both observe an unconfirmed
    state and commit, which would leave a confirmed submission whose
    nominations were mutated without the required reopen."""
    db, rule, entry, player_a, player_b, client = _context(postgres_url, 2403)
    submissions = OpeningRoundSubmissionRepository(db)
    nominations = OpeningRoundNominationRepository(db)

    def do_nominate():
        return nominations.nominate(
            rule.rule_id,
            entry.season_entry_id,
            "M1",
            player_a.season_player_id,
            client,
            actor=ActorContext.anonymous_operator("scorer"),
            reason="race",
        )

    def do_confirm():
        return submissions.confirm(
            rule.season_id, entry.season_entry_id, actor=ActorContext.anonymous_operator("admin")
        )

    nominate_result, confirm_result = race([do_nominate, do_confirm])
    assert confirm_result != "conflict"
    submission = submissions.get(rule.season_id, entry.season_entry_id)
    assert submission is not None and submission.state == "confirmed"
    persisted = nominations.list_for_season_entry(rule.season_id, entry.season_entry_id)
    if nominate_result == "conflict":
        # Confirmation won the race -- the nomination must have been
        # refused outright, never partially applied.
        assert persisted == []
    else:
        # The nomination won the race and committed before confirmation --
        # it must be exactly what the confirmed submission covers.
        assert [n.nomination_id for n in persisted] == [nominate_result.nomination_id]
