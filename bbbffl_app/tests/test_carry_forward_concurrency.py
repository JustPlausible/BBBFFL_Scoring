"""Production PostgreSQL serialization regressions for carry-forward and
scorer-proxy submissions -- both go through the exact same `weekly_lineup`
row lock/compare-and-swap boundary `WeeklyLineupRepository._finalize_
submission` already provides (see tests/test_lineups_concurrency.py for
the boundary this reuses rather than duplicates). A racing submission from
any source must never silently overwrite newer authoritative state."""

import os

import pytest

from app.audit import ActorContext
from app.carry_forward import CarryForwardService
from app.db import connect
from app.lineup_proxy import LineupProxyService
from app.lineups import WeeklyLineupRepository
from app.migrations import migrate
from tests.test_lineups_concurrency import race


@pytest.fixture(scope="module")
def postgres_url():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    return url


def postgres_context(url, year):
    from app.player_pool import OwnershipRepository, PlayerPoolRepository
    from app.round_mapping import RoundMappingRepository
    from app.season import SeasonRepository
    from tests.test_competition_lifecycle import KnownRound, operational

    db = connect(url)
    lifecycle, round1, entries = operational(db, year, year)
    scope = db.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round1.bbbffl_round_id,),
    ).fetchone()
    seasons, mappings = SeasonRepository(db), RoundMappingRepository(db)
    round2 = seasons.create_round(scope["competition_id"], "round-2", "Round 2", 2)
    mappings.accept(round2.bbbffl_round_id, year, year + 1, KnownRound(year, year + 1))
    lifecycle.create_ordinary_round(round2.bbbffl_round_id)
    lifecycle.transition(round1.bbbffl_round_id, "open")
    lifecycle.transition(round2.bbbffl_round_id, "open")
    OwnershipRepository(db).configure_squad_limit(scope["season_id"], 20)
    player = PlayerPoolRepository(db).refresh_player(scope["season_id"], year * 100, "Concurrent Player")
    OwnershipRepository(db).acquire(player.season_player_id, entries[0].season_entry_id)
    return db, round1, round2, entries, scope, player


def test_carry_forward_racing_a_concurrent_proxy_submission_has_one_winner(postgres_url):
    db, round1, round2, entries, scope, player = postgres_context(postgres_url, 2301)
    entry = entries[0]
    lineups = WeeklyLineupRepository(db)
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round1.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": player.season_player_id},
        expected_revision=0,
    )
    lineups.submit(draft.lineup_id, expected_draft_revision=1, expected_submission_version=0)

    carry_forward = CarryForwardService(db)
    proxy = LineupProxyService(db)
    proxy_draft = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        round2.bbbffl_round_id,
        entry.season_entry_id,
        {"M1": player.season_player_id},
        expected_revision=0,
        actor=ActorContext.anonymous_operator("scorer"),
    )

    def attempt_carry_forward():
        submitted, _ = carry_forward.carry_forward(
            scope["season_id"],
            scope["competition_id"],
            round2.bbbffl_round_id,
            entry.season_entry_id,
            expected_submission_version=0,
            actor=ActorContext.system(),
            reason="not submitted by lockout",
        )
        return submitted

    def attempt_proxy_submit():
        return proxy.submit(
            proxy_draft.lineup_id,
            expected_draft_revision=proxy_draft.revision,
            expected_submission_version=0,
            actor=ActorContext.anonymous_operator("scorer"),
            reason="proxy entry",
        )

    results = race([attempt_carry_forward, attempt_proxy_submit])
    assert sum(result == "conflict" for result in results) == 1
    winning_submission = next(result for result in results if result != "conflict")
    lineup_id, _ = lineups.get_or_create_header(
        scope["season_id"], scope["competition_id"], round2.bbbffl_round_id, entry.season_entry_id
    )
    effective = lineups.get_effective_submission(lineup_id)
    assert effective.version == winning_submission.version
    assert effective.source_type == winning_submission.source_type


def test_two_concurrent_carry_forward_attempts_have_one_winner(postgres_url):
    db, round1, round2, entries, scope, player = postgres_context(postgres_url, 2302)
    entry = entries[0]
    lineups = WeeklyLineupRepository(db)
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round1.bbbffl_round_id,
        entry.season_entry_id,
        {"F1": player.season_player_id},
        expected_revision=0,
    )
    lineups.submit(draft.lineup_id, expected_draft_revision=1, expected_submission_version=0)
    carry_forward = CarryForwardService(db)

    def attempt():
        submitted, _ = carry_forward.carry_forward(
            scope["season_id"],
            scope["competition_id"],
            round2.bbbffl_round_id,
            entry.season_entry_id,
            expected_submission_version=0,
            actor=ActorContext.system(),
            reason="not submitted by lockout",
        )
        return submitted

    results = race([attempt, attempt])
    assert sum(result == "conflict" for result in results) == 1
    lineup_id, _ = lineups.get_or_create_header(
        scope["season_id"], scope["competition_id"], round2.bbbffl_round_id, entry.season_entry_id
    )
    assert lineups.get_effective_submission(lineup_id).version == 1
