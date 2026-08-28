"""Deterministic 2026 replay coverage for carry-forward (roadmap package
22, issue #55's "2026 replay" requirement), following the same pattern as
`tests/test_lockouts.py`'s `test_deterministic_replay_with_a_persisted_
lockout_plan`/`test_2026_and_2027_lockout_plans_remain_independently_
scoped`: a deterministic, database-backed replay exercised through pytest
against a `year=2026` season, rather than a live interactive script --
this package adds no HTTP-routed lineup surface for an interactive replay
script to drive (see app/carry_forward.py's/app/lineup_proxy.py's module
docstrings and this issue's scope discipline).

SYNTHETIC EVIDENCE NOTICE: every player, lineup and round below is
synthetic test fixture data generated for this suite, not genuine
historical 2026 BBBFFL coach selections -- BBBFFL has no live afl-api
access to real 2026 season data from this environment (see
docs/afl-evidence-fixtures.md). Do not read anything here as a historical
record of what any real coach selected in 2026.
"""

import pytest

from app.audit import ActorContext
from app.carry_forward import CarryForwardService, NoCarryForwardSourceError, read_carry_forward_provenance
from app.lineups import WeeklyLineupRepository
from tests.test_carry_forward import acquire_players, context, submit_round

CARRY_FORWARD_ACTOR = ActorContext.anonymous_operator("scorer")


def test_2026_replay_a_later_round_left_deliberately_unsubmitted_is_carried_forward_exactly():
    """A later 2026 round intentionally has one team with no submission --
    a saved-but-unsubmitted round-2 draft exists, deliberately different
    from round 1's submitted lineup, exactly to prove it is ignored as a
    source. Demonstrates every element issue #55 asks the 2026 replay to
    show: (1) a previous valid submitted lineup exists; (2) the current
    round's private draft is ignored; (3) the carried-forward assignments
    are an exact copy; (4) provenance points at the prior round/version;
    (5) the result is identified as carried-forward, never coach-submitted.
    """
    db, _, rounds, entries, scope, pool, ownership = context(year=2026, rounds=2)
    entry = entries[0]
    players = acquire_players(pool, ownership, scope, entry, 1, 3)
    lineups = WeeklyLineupRepository(db)

    # (1) A previous valid submitted lineup exists (round 1, genuinely
    # submitted -- this is the "coach" source_type baseline).
    _, round1_submission = submit_round(
        lineups,
        scope,
        rounds[0],
        entry,
        {"F1": players[0].season_player_id, "M1": players[1].season_player_id},
    )
    assert round1_submission.source_type == "coach"

    # This team never submits round 2 by lockout, but *did* save a draft --
    # a different, deliberately distinguishable selection -- which must be
    # ignored as a carry-forward source (issue #55, design constraint).
    unsubmitted_draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        {"F1": players[2].season_player_id},
        expected_revision=0,
    )

    submitted, source = CarryForwardService(db).carry_forward(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entry.season_entry_id,
        expected_submission_version=0,
        actor=CARRY_FORWARD_ACTOR,
        reason="2026 replay: round 2 not submitted by lockout",
    )

    # (2) The unsubmitted round-2 draft was never read as a source, and
    # is left exactly as saved.
    still_draft = lineups.get_draft(scope["season_id"], scope["competition_id"], rounds[1], entry.season_entry_id)
    assert still_draft.revision == unsubmitted_draft.revision
    assert still_draft.positions["F1"] == players[2].season_player_id

    # (3) The carried-forward assignments are an exact copy of round 1's
    # submitted lineup -- not the ignored draft, not an optimised team.
    assert submitted.positions == round1_submission.positions
    assert submitted.positions["F1"] == players[0].season_player_id
    assert submitted.positions["M1"] == players[1].season_player_id

    # (4) Provenance points at round 1's own round/lineup/version identity.
    assert source.source_bbbffl_round_id == rounds[0]
    assert source.source_lineup_id == round1_submission.lineup_id
    assert source.source_version == round1_submission.version
    assert read_carry_forward_provenance(submitted) == source

    # (5) Never presented as a coach submission.
    assert submitted.source_type == "carry_forward"
    assert submitted.source_type != "coach"
    assert submitted.actor_role == "scorer"


def test_2026_replay_round_1_has_no_prior_lineup_and_requires_explicit_scorer_action():
    """The no-prior-lineup case, explicitly, for the 2026 replay: Round 1
    of the 2026 season has no predecessor at all -- carry-forward must
    refuse to invent a default/optimised team and instead surface an
    explicit state requiring scorer/admin confirmation or proxy entry."""
    db, _, rounds, entries, scope, pool, ownership = context(year=2026, rounds=1)
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
            reason="2026 replay: round 1 has no predecessor",
        )


def test_2026_replay_and_2027_live_season_state_remain_isolated():
    """A 2026 replay season and a 2027 "live" season sharing one database
    (as the real interactive replay setup does) never let one season's
    carry-forward resolution see the other's submissions, even with
    identical round sequence numbers and player identities drawn from
    separate season-scoped player pools."""
    db, _, rounds2026, entries2026, scope2026, pool2026, ownership2026 = context(year=2026, rounds=2)
    # A second, independent competition/season sharing the same database
    # connection -- mirrors a shared replay database holding both a 2026
    # replay season and 2027's live season.
    from tests.test_carry_forward import SCOPE_SQL
    from tests.test_competition_lifecycle import operational

    lifecycle2027, round2027, entries2027 = operational(db, 2027, 2027)
    lifecycle2027.transition(round2027.bbbffl_round_id, "open")
    scope2027 = db.execute(SCOPE_SQL, (round2027.bbbffl_round_id,)).fetchone()
    from app.player_pool import OwnershipRepository, PlayerPoolRepository

    pool2027, ownership2027 = PlayerPoolRepository(db), OwnershipRepository(db)
    ownership2027.configure_squad_limit(scope2027["season_id"], 20)

    lineups = WeeklyLineupRepository(db)
    entry2026, entry2027 = entries2026[0], entries2027[0]
    players2026 = acquire_players(pool2026, ownership2026, scope2026, entry2026, 1, 1)
    players2027 = acquire_players(pool2027, ownership2027, scope2027, entry2027, 1, 1)
    submit_round(lineups, scope2026, rounds2026[0], entry2026, {"F1": players2026[0].season_player_id})
    submit_round(lineups, scope2027, round2027.bbbffl_round_id, entry2027, {"F1": players2027[0].season_player_id})

    service = CarryForwardService(db)
    # 2027's round 1 has no 2026 predecessor to leak in from -- it is a
    # genuine first round of its own season/competition.
    assert (
        service.resolve_source(
            scope2027["season_id"], scope2027["competition_id"], round2027.bbbffl_round_id, entry2027.season_entry_id
        )
        is None
    )
    # 2026's own round 2 correctly resolves 2026's own round 1 -- proving
    # the isolation above is real scoping, not an accidental absence of
    # any source at all.
    resolved = service.resolve_source(
        scope2026["season_id"], scope2026["competition_id"], rounds2026[1], entry2026.season_entry_id
    )
    assert resolved is not None
    assert resolved.source_bbbffl_round_id == rounds2026[0]
