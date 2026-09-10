"""PostgreSQL regression coverage for issue #182.

`MidseasonDraftRepository.decide_trade`'s squad-capacity pre-check used to
run `SELECT COUNT(*) ... FOR UPDATE` against `player_ownership_period` (and
a second aggregate `FOR UPDATE` against `midseason_delisting` for the
covered-overage check). PostgreSQL rejects locking an aggregate result
outright (`psycopg.errors.FeatureNotSupported: FOR UPDATE is not allowed
with aggregate functions`), so approving any pending mid-season trade
crashed before it ever reached ownership -- SQLite's `_for_update_suffix`
no-op never surfaced this, which is why these tests run only against real
PostgreSQL (mirroring tests/test_draft_postgresql.py and
tests/test_preseason_postgresql.py's own PostgreSQL-only precedent).

The fix locks the receiving `season_entry` row first -- the same
parent-row lock `OwnershipRepository.acquire_in_transaction` already takes
before it validates squad capacity for every other acquisition path -- and
then counts the now-stable `player_ownership_period` rows without a lock
of their own. The `midseason_delisting` count needs no lock at all: the
whole `midseason_draft` row is already held `FOR UPDATE` for the duration
of `decide_trade` (via `_locked_draft`), and every delisting mutation
(`submit_delisting`, `withdraw_delisting`) takes that same lock before
touching the table, so it is already race-free.

These tests exercise `MidseasonDraftRepository.decide_trade` both directly
and through the exact CLI handlers `scripts.replay_2026_midseason_draft
decide-trade` dispatches to, proving: a valid trade applies atomically and
transitions pending -> approved; rejection still behaves; an over-capacity
approval fails safely with no partial ownership mutation; and the fixed
SQL path never re-issues `FOR UPDATE` against an aggregate query.
"""

import itertools
import os

import pytest
from sqlalchemy import event

from app.audit import ActorContext
from app.db import connect
from app.identity import IdentityRepository
from app.midseason_draft import MidseasonDraftRepository, MidseasonDraftStateError
from app.migrations import migrate
from app.season import SeasonRepository
from scripts.replay_2026_midseason_draft import COMMANDS, build_parser
from tests.midseason_draft_helpers import build_season

ACTOR = ActorContext.anonymous_operator("scorer")

_years = itertools.count(2260)


def _postgres_url():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL mid-season trade semantics require BBBFFL_DATABASE_URL")
    return url


@pytest.fixture
def postgres_season():
    """Ten entries (`app.fixtures.FixtureRepository.save_draft` requires
    exactly ten for its round-robin draw -- `build_season` always seeds
    that many), squad_limit=4 -- deliberately small so per-entry capacity
    edges (full vs. one spare) are cheap to set up and reason about; the
    dedicated 22-a-side fixture below covers the historical replay shape."""
    url = _postgres_url()
    migrate(url)
    database = connect(url)
    year = next(_years)
    ctx = build_season(database, year=year, trigger_round=10, squad_limit=4)
    SeasonRepository(database).set_midseason_draft_trigger_round(ctx["season"].season_id, 10)
    ctx["midseason"] = MidseasonDraftRepository(database)
    yield ctx
    database.close()


def _delisting_open(ctx):
    m = ctx["midseason"]
    m.confirm_ladder(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR)
    m.open_delisting_window(ctx["season"].season_id, actor=ACTOR)
    return ctx


def test_postgresql_valid_player_for_player_trade_approves_atomically_and_transfers_ownership(postgres_season):
    ctx = _delisting_open(postgres_season)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    squad_limit = ctx["squad_limit"]
    a, b = entries[0], entries[1]
    player_a = next(iter(m.ownership.current_squad(a.season_entry_id))).season_player_id
    player_b = next(iter(m.ownership.current_squad(b.season_entry_id))).season_player_id

    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": a.season_entry_id,
                "to_season_entry_id": b.season_entry_id,
                "season_player_id": player_a,
            },
            {
                "leg_type": "player",
                "from_season_entry_id": b.season_entry_id,
                "to_season_entry_id": a.season_entry_id,
                "season_player_id": player_b,
            },
        ],
        actor=ACTOR,
    )
    assert trade.status == "pending"

    decided = m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="approved by scorer")

    assert decided.status == "approved"
    assert m.get_trade(trade.trade_id).status == "approved"

    # Both legs applied -- squads stay within their limit, ownership moved
    # to the destination teams.
    assert len(m.ownership.current_squad(a.season_entry_id)) == squad_limit
    assert len(m.ownership.current_squad(b.season_entry_id)) == squad_limit
    assert player_b in {p.season_player_id for p in m.ownership.current_squad(a.season_entry_id)}
    assert player_a in {p.season_player_id for p in m.ownership.current_squad(b.season_entry_id)}
    assert player_a not in {p.season_player_id for p in m.ownership.current_squad(a.season_entry_id)}
    assert player_b not in {p.season_player_id for p in m.ownership.current_squad(b.season_entry_id)}

    # Previous ownership periods closed correctly.
    a_history = [p for p in m.ownership.history(player_a) if p.season_entry_id == a.season_entry_id]
    b_history = [p for p in m.ownership.history(player_b) if p.season_entry_id == b.season_entry_id]
    assert a_history and a_history[-1].released_at is not None
    assert b_history and b_history[-1].released_at is not None


def test_postgresql_rejecting_a_pending_trade_leaves_ownership_and_status_consistent(postgres_season):
    ctx = _delisting_open(postgres_season)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    a, b = entries[0], entries[1]
    player = next(iter(m.ownership.current_squad(a.season_entry_id))).season_player_id
    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": a.season_entry_id,
                "to_season_entry_id": b.season_entry_id,
                "season_player_id": player,
            }
        ],
        actor=ACTOR,
    )

    decided = m.decide_trade(season.season_id, trade.trade_id, False, actor=ACTOR, reason="declined")

    assert decided.status == "rejected"
    assert player in {p.season_player_id for p in m.ownership.current_squad(a.season_entry_id)}
    with pytest.raises(MidseasonDraftStateError):
        m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="too late")


def test_postgresql_over_capacity_approval_fails_atomically_with_no_partial_ownership_change(postgres_season):
    """A two-leg trade where the first leg is otherwise perfectly valid and
    the second leg's destination is already full with no covering
    delisting. `decide_trade` releases every leg before it acquires any --
    so if this test's capacity refusal on the *second* leg did not roll
    back the *first* leg's already-applied release, the fix would have
    traded atomicity for PostgreSQL-compatibility. It must not: the whole
    decision is one transaction, and MidseasonDraftStateError must unwind
    all of it."""
    ctx = _delisting_open(postgres_season)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    squad_limit = ctx["squad_limit"]
    a, b, c, d = entries[0], entries[1], entries[2], entries[3]
    assert len(m.ownership.current_squad(d.season_entry_id)) == squad_limit

    player_a = next(iter(m.ownership.current_squad(a.season_entry_id))).season_player_id
    player_c = next(iter(m.ownership.current_squad(c.season_entry_id))).season_player_id

    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": a.season_entry_id,
                "to_season_entry_id": b.season_entry_id,
                "season_player_id": player_a,
            },
            {
                "leg_type": "player",
                "from_season_entry_id": c.season_entry_id,
                "to_season_entry_id": d.season_entry_id,
                "season_player_id": player_c,
            },
        ],
        actor=ACTOR,
    )

    with pytest.raises(MidseasonDraftStateError):
        m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="second leg over capacity")

    assert m.get_trade(trade.trade_id).status == "pending"
    assert player_a in {p.season_player_id for p in m.ownership.current_squad(a.season_entry_id)}
    assert player_c in {p.season_player_id for p in m.ownership.current_squad(c.season_entry_id)}
    for entry in (a, b, c, d):
        assert len(m.ownership.current_squad(entry.season_entry_id)) == squad_limit


def test_postgresql_decide_trade_never_issues_for_update_on_an_aggregate_query(postgres_season):
    """Direct regression for issue #182's reported crash: capture every
    statement `decide_trade` sends to PostgreSQL and assert none combines
    an aggregate with `FOR UPDATE`. If that bug were reintroduced, this
    fails with a plain assertion here instead of only an opaque
    ProgrammingError from psycopg (which the other tests in this module
    would also catch, but less legibly)."""
    ctx = _delisting_open(postgres_season)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    a, b = entries[0], entries[1]
    player_a = next(iter(m.ownership.current_squad(a.season_entry_id))).season_player_id
    player_b = next(iter(m.ownership.current_squad(b.season_entry_id))).season_player_id
    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": a.season_entry_id,
                "to_season_entry_id": b.season_entry_id,
                "season_player_id": player_a,
            },
            {
                "leg_type": "player",
                "from_season_entry_id": b.season_entry_id,
                "to_season_entry_id": a.season_entry_id,
                "season_player_id": player_b,
            },
        ],
        actor=ACTOR,
    )

    statements = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    engine = ctx["database"].engine
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="approved by scorer")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    offending = [s for s in statements if "count(" in s.lower() and "for update" in s.lower()]
    assert not offending, f"decide_trade issued FOR UPDATE against an aggregate query: {offending}"


def test_postgresql_decide_trade_cli_path_approves_a_pending_trade(postgres_season):
    """Exercises the exact handlers `python -m
    scripts.replay_2026_midseason_draft trade` / `decide-trade` dispatch
    to (`COMMANDS["trade"]` / `COMMANDS["decide-trade"]`), against
    PostgreSQL, rather than only the repository method directly."""
    ctx = _delisting_open(postgres_season)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    a, b = entries[0], entries[1]
    player_a = next(iter(m.ownership.current_squad(a.season_entry_id))).season_player_id
    player_b = next(iter(m.ownership.current_squad(b.season_entry_id))).season_player_id

    parser = build_parser()
    trade_args = parser.parse_args(
        [
            "--database-url",
            "postgresql://unused-cli-commands-reuse-the-bound-repository",
            "trade",
            "--season-id",
            season.season_id,
            "--leg",
            f"player:{a.season_entry_id}:{b.season_entry_id}:{player_a}",
            "--leg",
            f"player:{b.season_entry_id}:{a.season_entry_id}:{player_b}",
            "--reason",
            "cli replay trade",
        ]
    )
    assert COMMANDS["trade"](m, trade_args) == 0
    trade = next(t for t in m.list_trades(season.season_id) if t.status == "pending")

    decide_args = parser.parse_args(
        [
            "--database-url",
            "postgresql://unused-cli-commands-reuse-the-bound-repository",
            "decide-trade",
            "--season-id",
            season.season_id,
            "--trade-id",
            trade.trade_id,
            "--approve",
            "--reason",
            "cli replay approval",
        ]
    )
    assert COMMANDS["decide-trade"](m, decide_args) == 0
    assert m.get_trade(trade.trade_id).status == "approved"
    assert player_b in {p.season_player_id for p in m.ownership.current_squad(a.season_entry_id)}
    assert player_a in {p.season_player_id for p in m.ownership.current_squad(b.season_entry_id)}


@pytest.fixture
def replay_acceptance_season():
    """Mirrors issue #182's replay reproduction: ten squads of 22, the
    mid-season draft already through to `draft_complete` (post-draft
    trading open) with nobody delisted -- the same trivial-completion
    shape as tests/test_midseason_draft.py's
    test_lock_delistings_and_generate_selection_table_complete_trivially_when_nobody_delists.
    Two entries and two players are renamed to the historical replay's
    "The Crabs" / "Bridesmaids" and "James Rowbottom" / "Lachlan
    McAndrew" so the acceptance test below reads as the actual documented
    trade, not just an anonymous swap."""
    url = _postgres_url()
    migrate(url)
    database = connect(url)
    year = next(_years)
    ctx = build_season(database, year=year, entry_count=10, trigger_round=10, squad_limit=22)
    season = ctx["season"]
    SeasonRepository(database).set_midseason_draft_trigger_round(season.season_id, 10)
    m = MidseasonDraftRepository(database)
    ctx["midseason"] = m

    identities = IdentityRepository(database)
    crabs, bridesmaids = ctx["entries"][0], ctx["entries"][1]
    identities.rename_team(crabs.season_entry_id, "The Crabs", actor=ACTOR)
    identities.rename_team(bridesmaids.season_entry_id, "Bridesmaids", actor=ACTOR)
    rowbottom = next(iter(m.ownership.current_squad(crabs.season_entry_id))).season_player_id
    mcandrew = next(iter(m.ownership.current_squad(bridesmaids.season_entry_id))).season_player_id
    database.execute(
        "UPDATE season_player_pool SET display_name=? WHERE season_player_id=?",
        ("James Rowbottom", rowbottom),
    )
    database.execute(
        "UPDATE season_player_pool SET display_name=? WHERE season_player_id=?",
        ("Lachlan McAndrew", mcandrew),
    )

    m.confirm_ladder(season.season_id, ctx["competition"].competition_id, actor=ACTOR)
    m.open_delisting_window(season.season_id, actor=ACTOR)
    m.lock_delistings(season.season_id, actor=ACTOR)
    draft = m.generate_selection_table(season.season_id, actor=ACTOR)
    assert draft.state == "draft_complete"

    ctx["crabs"] = crabs
    ctx["bridesmaids"] = bridesmaids
    ctx["rowbottom"] = rowbottom
    ctx["mcandrew"] = mcandrew
    yield ctx
    database.close()


def test_postgresql_replay_acceptance_rowbottom_mcandrew_trade_keeps_both_squads_at_22(replay_acceptance_season):
    ctx = replay_acceptance_season
    m, season = ctx["midseason"], ctx["season"]
    crabs, bridesmaids = ctx["crabs"], ctx["bridesmaids"]
    rowbottom, mcandrew = ctx["rowbottom"], ctx["mcandrew"]

    assert len(m.ownership.current_squad(crabs.season_entry_id)) == 22
    assert len(m.ownership.current_squad(bridesmaids.season_entry_id)) == 22

    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": crabs.season_entry_id,
                "to_season_entry_id": bridesmaids.season_entry_id,
                "season_player_id": rowbottom,
            },
            {
                "leg_type": "player",
                "from_season_entry_id": bridesmaids.season_entry_id,
                "to_season_entry_id": crabs.season_entry_id,
                "season_player_id": mcandrew,
            },
        ],
        actor=ACTOR,
        reason="James Rowbottom (Crabs->Bridesmaids) for Lachlan McAndrew (Bridesmaids->Crabs)",
    )

    decided = m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="approved by scorer")

    assert decided.status == "approved"
    assert len(m.ownership.current_squad(crabs.season_entry_id)) == 22
    assert len(m.ownership.current_squad(bridesmaids.season_entry_id)) == 22
    assert mcandrew in {p.season_player_id for p in m.ownership.current_squad(crabs.season_entry_id)}
    assert rowbottom in {p.season_player_id for p in m.ownership.current_squad(bridesmaids.season_entry_id)}

    # Post-draft trading can subsequently close normally.
    completed = m.close_post_draft_trading(season.season_id, actor=ACTOR, reason="post-draft trading closed")
    assert completed.state == "complete"
