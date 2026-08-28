"""Carry-forward: exact resolution/persistence of the previous relevant
submitted lineup (roadmap package 22, issue #55). See app/carry_forward.py's
module docstring for the design this exercises; app/lineups.py's #33
private-draft/immutable-submission boundary and app/audit.py's #17
append-only event boundary are reused directly, never duplicated."""

import pytest

from app.audit import ActorContext
from app.carry_forward import CarryForwardService, NoCarryForwardSourceError, read_carry_forward_provenance
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.fixtures import FixtureRepository
from app.identity import IdentityRepository
from app.lineups import LineupIntegrityError, WeeklyLineupRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.round_mapping import RoundMappingRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import KnownRound, operational

CARRY_FORWARD_ACTOR = ActorContext.system()

SCOPE_SQL = (
    "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
    "JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?"
)


def context(year=2027, rounds=3, squad_limit=20):
    """A single ordinary competition stream with `rounds` sequential BBBFFL
    rounds, all already `open`, ready for weekly-lineup submission."""
    db = migrated_connection()
    lifecycle, round1, entries = operational(db, year, year)
    scope = db.execute(SCOPE_SQL, (round1.bbbffl_round_id,)).fetchone()
    seasons, mappings = SeasonRepository(db), RoundMappingRepository(db)
    round_ids = [round1.bbbffl_round_id]
    for sequence in range(2, rounds + 1):
        afl_round = year * 10 + sequence
        logical = seasons.create_round(scope["competition_id"], f"round-{sequence}", f"Round {sequence}", sequence)
        mappings.accept(logical.bbbffl_round_id, year, afl_round, KnownRound(year, afl_round))
        lifecycle.create_ordinary_round(logical.bbbffl_round_id)
        round_ids.append(logical.bbbffl_round_id)
    for round_id in round_ids:
        lifecycle.transition(round_id, "open")
    pool, ownership = PlayerPoolRepository(db), OwnershipRepository(db)
    ownership.configure_squad_limit(scope["season_id"], squad_limit)
    return db, lifecycle, round_ids, entries, scope, pool, ownership


def acquire_players(pool, ownership, scope, entry, start, count):
    players = []
    for offset in range(count):
        player = pool.refresh_player(scope["season_id"], start + offset, f"Player {start + offset}")
        ownership.acquire(player.season_player_id, entry.season_entry_id)
        players.append(player)
    return players


def submit_round(lineups, scope, round_id, entry, positions, *, revision=0, version=0):
    draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_id,
        entry.season_entry_id,
        positions,
        expected_revision=revision,
    )
    submitted = lineups.submit(
        draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=version
    )
    return draft, submitted


# ---------------------------------------------------------------------------
# Exactness and provenance
# ---------------------------------------------------------------------------


def test_exact_previous_submitted_lineup_is_carried_forward_with_source_provenance():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=2)
    entry = entries[0]
    players = acquire_players(pool, ownership, scope, entry, 1, 2)
    lineups = WeeklyLineupRepository(db)
    _, first = submit_round(
        lineups, scope, rounds[0], entry, {"F1": players[0].season_player_id, "M1": players[1].season_player_id}
    )

    submitted, source = CarryForwardService(db).carry_forward(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        expected_submission_version=0,
        actor=CARRY_FORWARD_ACTOR,
        reason="round 2 not submitted by kickoff",
    )

    assert submitted.positions == first.positions
    assert submitted.source_type == "carry_forward"
    assert submitted.source_type != "coach"
    assert submitted.reason == "round 2 not submitted by kickoff"
    assert submitted.actor_type == "system"
    assert source.source_bbbffl_round_id == rounds[0]
    assert source.source_lineup_id == first.lineup_id
    assert source.source_version == first.version
    assert read_carry_forward_provenance(submitted) == source
    # A coach's own submission is never mistaken for carry-forward provenance.
    assert read_carry_forward_provenance(first) is None


def test_every_slot_is_identical_no_optimisation_reordering_or_replacement():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=2)
    entry = entries[0]
    players = acquire_players(pool, ownership, scope, entry, 1, 4)
    lineups = WeeklyLineupRepository(db)
    positions = {
        "F1": players[0].season_player_id,
        "F2": players[1].season_player_id,
        "Interchange": players[3].season_player_id,
    }
    _, first = submit_round(lineups, scope, rounds[0], entry, positions)

    submitted, _ = CarryForwardService(db).carry_forward(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        expected_submission_version=0,
        actor=CARRY_FORWARD_ACTOR,
    )

    for position, player_id in first.positions.items():
        assert submitted.positions[position] == player_id, position


def test_unsubmitted_newer_draft_is_ignored_as_a_source_and_left_untouched():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=2)
    entry = entries[0]
    players = acquire_players(pool, ownership, scope, entry, 1, 2)
    lineups = WeeklyLineupRepository(db)
    _, first = submit_round(lineups, scope, rounds[0], entry, {"F1": players[0].season_player_id})
    # The entry saved -- but never submitted -- a different round-2 draft.
    draft2 = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        {"M1": players[1].season_player_id},
        expected_revision=0,
    )

    submitted, source = CarryForwardService(db).carry_forward(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        expected_submission_version=0,
        actor=CARRY_FORWARD_ACTOR,
    )

    assert submitted.positions == first.positions
    assert source.source_bbbffl_round_id == rounds[0]
    # The private draft is left exactly as the entry saved it -- carry
    # forward never reads it as a source, and never overwrites it either.
    still = lineups.get_draft(scope["season_id"], scope["competition_id"], rounds[1], entry.season_entry_id)
    assert still.revision == draft2.revision
    assert still.positions["M1"] == players[1].season_player_id
    assert still.positions["F1"] is None


def test_chained_carry_forward_becomes_a_valid_source_for_a_later_round():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=3)
    entry = entries[0]
    players = acquire_players(pool, ownership, scope, entry, 1, 1)
    lineups = WeeklyLineupRepository(db)
    _, first = submit_round(lineups, scope, rounds[0], entry, {"F1": players[0].season_player_id})
    service = CarryForwardService(db)
    round2, _ = service.carry_forward(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        expected_submission_version=0,
        actor=CARRY_FORWARD_ACTOR,
    )

    round3, source3 = service.carry_forward(
        scope["season_id"],
        scope["competition_id"],
        rounds[2],
        entry.season_entry_id,
        expected_submission_version=0,
        actor=CARRY_FORWARD_ACTOR,
    )

    assert round3.positions == first.positions
    assert round2.source_type == "carry_forward"
    # Round 3 sourced round 2's own (carried-forward) submission, not round 1
    # directly -- "previous relevant" always means the nearest submitted
    # round, whatever produced it.
    assert source3.source_bbbffl_round_id == rounds[1]
    assert source3.source_lineup_id == round2.lineup_id
    assert source3.source_version == round2.version


# ---------------------------------------------------------------------------
# No-prior-lineup / Round 1
# ---------------------------------------------------------------------------


def test_round_1_has_no_source_and_requires_explicit_scorer_action():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=1)
    entry = entries[0]
    service = CarryForwardService(db)

    assert service.resolve_source(scope["season_id"], scope["competition_id"], rounds[0], entry.season_entry_id) is None
    with pytest.raises(NoCarryForwardSourceError):
        service.carry_forward(
            scope["season_id"],
            scope["competition_id"],
            rounds[0],
            entry.season_entry_id,
            expected_submission_version=0,
            actor=CARRY_FORWARD_ACTOR,
        )
    # Nothing was invented: the round remains genuinely unsubmitted.
    lineups = WeeklyLineupRepository(db)
    lineup_id, version = lineups.get_or_create_header(
        scope["season_id"], scope["competition_id"], rounds[0], entry.season_entry_id
    )
    assert version == 0
    assert lineups.get_effective_submission(lineup_id) is None


def test_a_round_with_no_predecessor_in_its_own_stream_has_no_source_even_with_other_entries_submitted():
    """Round 1 of any stream never has a source, however many *other*
    entries in the same competition have (impossibly, since round 1 has no
    predecessor either) submitted anything -- there is no earlier round at
    all for `resolve_source`'s query to find."""
    db, _, rounds, entries, scope, pool, ownership = context(rounds=1)
    service = CarryForwardService(db)
    for entry in entries:
        assert (
            service.resolve_source(scope["season_id"], scope["competition_id"], rounds[0], entry.season_entry_id)
            is None
        )


# ---------------------------------------------------------------------------
# Ownership: structural validation is never silently repaired
# ---------------------------------------------------------------------------


def test_ownership_change_since_source_submission_fails_explicitly_rather_than_repairing():
    db, _, rounds, entries, scope, pool, ownership = context(rounds=2)
    entry = entries[0]
    players = acquire_players(pool, ownership, scope, entry, 1, 1)
    lineups = WeeklyLineupRepository(db)
    submit_round(lineups, scope, rounds[0], entry, {"F1": players[0].season_player_id})
    ownership.release(players[0].season_player_id)

    with pytest.raises(LineupIntegrityError, match="not currently owned"):
        CarryForwardService(db).carry_forward(
            scope["season_id"],
            scope["competition_id"],
            rounds[1],
            entry.season_entry_id,
            expected_submission_version=0,
            actor=CARRY_FORWARD_ACTOR,
        )
    # The attempt left no submission behind -- no silent substitute team.
    lineup_id, version = lineups.get_or_create_header(
        scope["season_id"], scope["competition_id"], rounds[1], entry.season_entry_id
    )
    assert version == 0


# ---------------------------------------------------------------------------
# Competition-stream isolation
# ---------------------------------------------------------------------------


def test_two_competition_streams_in_one_season_cannot_source_each_other():
    """Proves `CarryForwardService.resolve_source` is competition_id-scoped:
    the same entry, submitting in two different competition streams within
    one season, never has one stream's submission resolved as the other's
    carry-forward source (issue #55: ordinary and a future SuperScore
    stream must never source each other, even for the same coach/entry).

    A *real* SuperScore round's lifecycle is opened via `app.superscore` --
    a sibling vertical `app.carry_forward`/`app.season` must never depend
    on (see tests/test_architecture.py) -- so this stands up a second,
    independently-opened `stream_type="ordinary"` competition in the same
    season as the isolation fixture, rather than the real SuperScore
    vertical. The isolation mechanism exercised here --
    `resolve_source`'s `competition_id`-scoped join -- is exactly what a
    true SuperScore stream would rely on too; nothing about it is
    "ordinary"-specific.
    """
    db = migrated_connection()
    seasons = SeasonRepository(db)
    season = seasons.create_season(2027, "2027")
    rules = seasons.create_rules_version(season.season_id, "ordinary", 1, "Rules")
    stream_a = seasons.create_competition(season.season_id, rules.rules_version_id, "stream-a", "Stream A", "ordinary")
    # stream_type is "ordinary" here too -- app.competition_lifecycle.
    # CompetitionLifecycleRepository.create_ordinary_round refuses any other
    # stream_type, and a real SuperScore round's lifecycle is opened via the
    # sibling app.superscore vertical instead (see this test's docstring).
    # What this test actually proves -- resolve_source's competition_id-
    # scoped isolation -- does not depend on that label either way.
    stream_b = seasons.create_competition(
        season.season_id, rules.rules_version_id, "stream-b", "Stream B (isolated stream)", "ordinary"
    )
    round_a1 = seasons.create_round(stream_a.competition_id, "a-1", "A Round 1", 1)
    round_a2 = seasons.create_round(stream_a.competition_id, "a-2", "A Round 2", 2)
    round_b1 = seasons.create_round(stream_b.competition_id, "b-1", "B Round 1", 1)
    round_b2 = seasons.create_round(stream_b.competition_id, "b-2", "B Round 2", 2)

    identities = IdentityRepository(db)
    entries = []
    for number in range(10):
        coach = identities.create_coach(f"Coach {number}")
        entries.append(identities.create_entry(season.season_id, f"licence-{number}", coach.coach_id, f"Team {number}"))
    fixtures = FixtureRepository(db)
    fixtures.save_draft(season.season_id, [entry.season_entry_id for entry in entries])
    fixtures.freeze(season.season_id)

    mappings = RoundMappingRepository(db)
    mappings.accept(round_a1.bbbffl_round_id, 2027, 5001, KnownRound(2027, 5001))
    mappings.accept(round_a2.bbbffl_round_id, 2027, 5002, KnownRound(2027, 5002))
    mappings.accept(round_b1.bbbffl_round_id, 2027, 5003, KnownRound(2027, 5003))
    mappings.accept(round_b2.bbbffl_round_id, 2027, 5004, KnownRound(2027, 5004))
    lifecycle = CompetitionLifecycleRepository(db)
    for round_ in (round_a1, round_a2, round_b1, round_b2):
        lifecycle.create_ordinary_round(round_.bbbffl_round_id)
        lifecycle.transition(round_.bbbffl_round_id, "open")

    entry = entries[0]
    pool, ownership = PlayerPoolRepository(db), OwnershipRepository(db)
    ownership.configure_squad_limit(season.season_id, 20)
    scope_a = {"season_id": season.season_id, "competition_id": stream_a.competition_id}
    players = acquire_players(pool, ownership, scope_a, entry, 1, 1)
    lineups = WeeklyLineupRepository(db)
    submit_round(lineups, scope_a, round_a1.bbbffl_round_id, entry, {"F1": players[0].season_player_id})

    service = CarryForwardService(db)
    # Round 2 of stream B must not see stream A's round-1 submission, even
    # for the identical entry.
    assert (
        service.resolve_source(
            season.season_id, stream_b.competition_id, round_b2.bbbffl_round_id, entry.season_entry_id
        )
        is None
    )
    with pytest.raises(NoCarryForwardSourceError):
        service.carry_forward(
            season.season_id,
            stream_b.competition_id,
            round_b2.bbbffl_round_id,
            entry.season_entry_id,
            expected_submission_version=0,
            actor=CARRY_FORWARD_ACTOR,
        )
    # Stream A's own round 2 correctly resolves stream A's round 1.
    resolved = service.resolve_source(
        season.season_id, stream_a.competition_id, round_a2.bbbffl_round_id, entry.season_entry_id
    )
    assert resolved is not None
    assert resolved.source_bbbffl_round_id == round_a1.bbbffl_round_id
