"""Preseason transaction/finalisation window: lifecycle, atomic trades,
ownership integrity, squad validation, the frozen opening snapshot, audit
and closed-window rejection (roadmap package 15, issue #54). Builds directly
on the finalised draft covered by tests/test_draft.py and
tests/test_draft_operations.py rather than re-testing the draft itself."""

import pytest

from app.audit import ActorContext, AuditEventRepository
from app.draft import DraftRepository
from app.identity import IdentityRepository
from app.player_pool import (
    OwnershipRepository,
    PlayerPoolRepository,
    PreseasonWindowClosedError,
    SquadCapacityError,
)
from app.preseason import (
    PreseasonDraftNotFinalizedError,
    PreseasonRepository,
    PreseasonSnapshotError,
    PreseasonSquadValidationError,
    PreseasonStateError,
    PreseasonTradeValidationError,
    PreseasonWindowExistsError,
)
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


def domain(entries=3, limit=2, extra_players=0, finalize_draft=True, year=2027):
    """A finalised draft (or, with `finalize_draft=False`, a merely accepted
    one) for `entries` entries of `limit` players each, plus `extra_players`
    additional undrafted free agents -- everything a preseason window test
    needs, without re-deriving draft mechanics already covered elsewhere."""
    db = migrated_connection()
    season = SeasonRepository(db).create_season(year, f"{year}")
    identities = IdentityRepository(db)
    season_entries = [
        identities.create_entry(
            season.season_id, f"licence-{number}", identities.create_coach(f"Coach {number}").coach_id, f"Team {number}"
        )
        for number in range(entries)
    ]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(season.season_id, limit)
    pool = PlayerPoolRepository(db)
    players = [
        pool.refresh_player(season.season_id, number + 1, f"Player {number}")
        for number in range(entries * limit + extra_players)
    ]
    draft = DraftRepository(db)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in season_entries])
    if finalize_draft:
        for _ in range(entries * limit):
            pick = draft.next_pick(season.season_id)
            draft.execute_pick(
                season.season_id, pick.current_season_entry_id, players[pick.overall_number - 1].season_player_id
            )
        draft.finalize(season.season_id)
    preseason = PreseasonRepository(db)
    return db, season, season_entries, players, ownership, pool, draft, preseason


def squad(ownership, entry_id):
    return [period.season_player_id for period in ownership.squad_at(entry_id, "9999-01-01")]


# -- Lifecycle ---------------------------------------------------------------


def test_finalized_draft_can_open_the_preseason_window():
    _db, season, _entries, _players, _ownership, _pool, draft, preseason = domain()
    window = preseason.open_window(season.season_id, reason="preseason trading begins")
    assert window.is_open
    assert window.draft_id == draft.status(season.season_id).draft_id


def test_unfinalized_or_missing_draft_cannot_open_the_window():
    _db, season, _entries, _players, _ownership, _pool, _draft, preseason = domain(finalize_draft=False)
    with pytest.raises(PreseasonDraftNotFinalizedError):
        preseason.open_window(season.season_id)


def test_a_season_with_no_draft_at_all_cannot_open_the_window():
    db = migrated_connection()
    season = SeasonRepository(db).create_season(2099, "no draft")
    preseason = PreseasonRepository(db)
    with pytest.raises(PreseasonDraftNotFinalizedError):
        preseason.open_window(season.season_id)


def test_opening_the_window_twice_is_rejected():
    _db, season, _entries, _players, _ownership, _pool, _draft, preseason = domain()
    preseason.open_window(season.season_id)
    with pytest.raises(PreseasonWindowExistsError):
        preseason.open_window(season.season_id)


def test_window_can_be_explicitly_closed_and_closing_again_is_rejected():
    _db, season, _entries, _players, _ownership, _pool, _draft, preseason = domain()
    preseason.open_window(season.season_id)
    closed = preseason.close_window(season.season_id, reason="opening squads locked")
    assert not closed.is_open
    with pytest.raises(PreseasonWindowClosedError):
        preseason.close_window(season.season_id)


def test_closed_window_rejects_trades_and_direct_ownership_mutation():
    """Rejection must live below the route/UI layer: both `submit_trade`
    and a completely direct `OwnershipRepository` call (the "existing
    lower-level mutation path" the issue calls out) must fail once closed."""
    _db, season, entries, players, ownership, _pool, _draft, preseason = domain(entries=2, limit=1, extra_players=1)
    preseason.open_window(season.season_id)
    preseason.close_window(season.season_id)

    e0, e1 = (entry.season_entry_id for entry in entries)
    p0 = squad(ownership, e0)[0]
    with pytest.raises(PreseasonWindowClosedError):
        preseason.submit_trade(
            season.season_id,
            [{"season_player_id": p0, "from_season_entry_id": e0, "to_season_entry_id": e1}],
        )
    with pytest.raises(PreseasonWindowClosedError):
        ownership.transfer(p0, e1)
    with pytest.raises(PreseasonWindowClosedError):
        ownership.release(p0)
    free_agent = players[-1].season_player_id
    with pytest.raises(PreseasonWindowClosedError):
        ownership.acquire(free_agent, e0)


def _seed_season(db, year, *, entries=2, limit=1):
    """Build one finalised-draft season on an already-open `db` connection
    -- like `domain()`, but sharing a caller-supplied connection so two
    seasons can be proven isolated within a single database."""
    season = SeasonRepository(db).create_season(year, f"{year}")
    identities = IdentityRepository(db)
    season_entries = [
        identities.create_entry(
            season.season_id,
            f"licence-{year}-{n}",
            identities.create_coach(f"Coach {year}-{n}").coach_id,
            f"Team {year}-{n}",
        )
        for n in range(entries)
    ]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(season.season_id, limit)
    pool = PlayerPoolRepository(db)
    players = [
        pool.refresh_player(season.season_id, year * 1000 + n, f"Player {year}-{n}") for n in range(entries * limit)
    ]
    draft = DraftRepository(db)
    draft.accept_order(season.season_id, [entry.season_entry_id for entry in season_entries])
    for _ in range(entries * limit):
        pick = draft.next_pick(season.season_id)
        draft.execute_pick(
            season.season_id, pick.current_season_entry_id, players[pick.overall_number - 1].season_player_id
        )
    draft.finalize(season.season_id)
    return season, season_entries, ownership


def test_one_seasons_window_does_not_affect_another():
    """Two seasons sharing one database: closing season A's window must not
    prevent season B's window from opening, trading, or eventually closing
    on its own terms -- lifecycle state is season-scoped, not global."""
    db = migrated_connection()
    season_a, _entries_a, _ownership_a = _seed_season(db, 2026)
    season_b, entries_b, ownership_b = _seed_season(db, 2027)
    preseason = PreseasonRepository(db)

    preseason.open_window(season_a.season_id)
    preseason.close_window(season_a.season_id)
    assert not preseason.get_window(season_a.season_id).is_open

    window_b = preseason.open_window(season_b.season_id)
    assert window_b.is_open
    e0, e1 = (entry.season_entry_id for entry in entries_b)
    p0 = squad(ownership_b, e0)[0]
    p1 = squad(ownership_b, e1)[0]
    trade = preseason.submit_trade(
        season_b.season_id,
        [
            {"season_player_id": p0, "from_season_entry_id": e0, "to_season_entry_id": e1},
            {"season_player_id": p1, "from_season_entry_id": e1, "to_season_entry_id": e0},
        ],
    )
    assert trade is not None
    assert preseason.get_window(season_b.season_id).is_open

    closed_b = preseason.close_window(season_b.season_id)
    assert not closed_b.is_open
    assert not preseason.get_window(season_a.season_id).is_open


# -- Two-club trade ------------------------------------------------------


def test_valid_two_club_trade_succeeds_and_ownership_and_audit_reflect_it():
    db, season, entries, _players, ownership, _pool, _draft, preseason = domain(entries=2, limit=2)
    preseason.open_window(season.season_id)
    e0, e1 = (entry.season_entry_id for entry in entries)
    p0, p0b = squad(ownership, e0)
    p1, _p1b = squad(ownership, e1)

    trade = preseason.submit_trade(
        season.season_id,
        [
            {"season_player_id": p0, "from_season_entry_id": e0, "to_season_entry_id": e1},
            {"season_player_id": p1, "from_season_entry_id": e1, "to_season_entry_id": e0},
        ],
        actor=ActorContext.anonymous_operator("scorer"),
        reason="agreed swap",
    )

    assert set(squad(ownership, e0)) == {p0b, p1}
    assert p0 in squad(ownership, e1)
    legs = preseason.trade_legs(trade.trade_id)
    assert {leg.season_player_id for leg in legs} == {p0, p1}
    assert trade.reason == "agreed swap"

    events = AuditEventRepository(db).list_events(action="preseason.trade.applied")
    assert len(events) == 1
    assert events[0].reason == "agreed swap"
    assert events[0].actor_role == "scorer"

    # Provenance: each leg links to the exact ownership-ledger rows it produced.
    for leg in legs:
        released = ownership.history(leg.season_player_id)
        assert any(period.ownership_period_id == leg.released_ownership_period_id for period in released)
        assert any(period.ownership_period_id == leg.acquired_ownership_period_id for period in released)


# -- Multi-club trade ------------------------------------------------------


def test_three_club_rotation_is_one_atomic_trade():
    _db, season, entries, _players, ownership, _pool, _draft, preseason = domain(entries=3, limit=2)
    preseason.open_window(season.season_id)
    e0, e1, e2 = (entry.season_entry_id for entry in entries)
    p0 = squad(ownership, e0)[0]
    p1 = squad(ownership, e1)[0]
    p2 = squad(ownership, e2)[0]

    trade = preseason.submit_trade(
        season.season_id,
        [
            {"season_player_id": p0, "from_season_entry_id": e0, "to_season_entry_id": e1},
            {"season_player_id": p1, "from_season_entry_id": e1, "to_season_entry_id": e2},
            {"season_player_id": p2, "from_season_entry_id": e2, "to_season_entry_id": e0},
        ],
        reason="three club rotation",
    )

    legs = preseason.trade_legs(trade.trade_id)
    assert len(legs) == 3
    assert p1 in squad(ownership, e2)
    assert p2 in squad(ownership, e0)
    assert p0 in squad(ownership, e1)
    for entry_id in (e0, e1, e2):
        assert len(squad(ownership, entry_id)) == 2


def test_multiple_players_moving_between_the_same_two_entries_in_one_trade():
    """A balanced two-for-two swap: both entries stay at their configured
    squad size throughout, so this exercises "several players in one
    logical trade" without also exercising the separate squad-capacity
    atomicity case covered above."""
    _db, season, entries, _players, ownership, _pool, _draft, preseason = domain(entries=2, limit=3)
    preseason.open_window(season.season_id)
    e0, e1 = (entry.season_entry_id for entry in entries)
    p0a, p0b, _p0c = squad(ownership, e0)
    p1a, p1b, _p1c = squad(ownership, e1)

    trade = preseason.submit_trade(
        season.season_id,
        [
            {"season_player_id": p0a, "from_season_entry_id": e0, "to_season_entry_id": e1},
            {"season_player_id": p0b, "from_season_entry_id": e0, "to_season_entry_id": e1},
            {"season_player_id": p1a, "from_season_entry_id": e1, "to_season_entry_id": e0},
            {"season_player_id": p1b, "from_season_entry_id": e1, "to_season_entry_id": e0},
        ],
        reason="two for two",
    )
    assert len(preseason.trade_legs(trade.trade_id)) == 4
    assert len(squad(ownership, e0)) == 3 and len(squad(ownership, e1)) == 3
    assert {p0a, p0b} <= set(squad(ownership, e1))
    assert {p1a, p1b} <= set(squad(ownership, e0))


# -- Atomic rollback -------------------------------------------------------


def test_a_trade_with_one_invalid_leg_leaves_no_ownership_changed_and_no_misleading_history():
    db, season, entries, _players, ownership, _pool, _draft, preseason = domain(entries=2, limit=2)
    preseason.open_window(season.season_id)
    e0, e1 = (entry.season_entry_id for entry in entries)
    p0, p0b = squad(ownership, e0)
    p1, _p1b = squad(ownership, e1)

    before_p0 = ownership.history(p0)
    before_p1 = ownership.history(p1)
    before_p0b = ownership.history(p0b)

    with pytest.raises(PreseasonTradeValidationError) as excinfo:
        preseason.submit_trade(
            season.season_id,
            [
                {"season_player_id": p0, "from_season_entry_id": e0, "to_season_entry_id": e1},
                {"season_player_id": p1, "from_season_entry_id": e1, "to_season_entry_id": e0},
                # Third leg is invalid: p0b is not owned by e1.
                {"season_player_id": p0b, "from_season_entry_id": e1, "to_season_entry_id": e0},
            ],
            reason="would-be trade",
        )
    assert excinfo.value.issues

    assert ownership.history(p0) == before_p0
    assert ownership.history(p1) == before_p1
    assert ownership.history(p0b) == before_p0b
    assert set(squad(ownership, e0)) == {p0, p0b}
    assert preseason.list_trades(season.season_id) == []

    events = AuditEventRepository(db).list_events(action="preseason.trade.applied")
    assert events == []


def test_a_squad_capacity_violation_discovered_only_while_applying_the_trade_rolls_back_completely():
    """`submit_trade`'s own pre-write validation pass deliberately does not
    duplicate the ownership ledger's squad-capacity check -- it is only
    discovered once `acquire_in_transaction` actually runs, by which point
    this leg's `release_in_transaction` has already executed inside the
    same transaction. That earlier write must still be rolled back with
    everything else."""
    _db, season, entries, _players, ownership, _pool, _draft, preseason = domain(entries=2, limit=1)
    preseason.open_window(season.season_id)
    e0, e1 = (entry.season_entry_id for entry in entries)
    p0 = squad(ownership, e0)[0]
    p1 = squad(ownership, e1)[0]  # e1 is already at its squad limit of 1.

    with pytest.raises(SquadCapacityError):
        preseason.submit_trade(
            season.season_id,
            [{"season_player_id": p0, "from_season_entry_id": e0, "to_season_entry_id": e1}],
        )

    assert squad(ownership, e0) == [p0]
    assert squad(ownership, e1) == [p1]
    assert preseason.list_trades(season.season_id) == []


# -- Ownership integrity ---------------------------------------------------


def test_transfer_by_a_non_owner_is_rejected():
    _db, season, entries, _players, ownership, _pool, _draft, preseason = domain(entries=3, limit=1)
    preseason.open_window(season.season_id)
    e0, e1, e2 = (entry.season_entry_id for entry in entries)
    p0 = squad(ownership, e0)[0]

    with pytest.raises(PreseasonTradeValidationError) as excinfo:
        preseason.submit_trade(
            season.season_id,
            [{"season_player_id": p0, "from_season_entry_id": e2, "to_season_entry_id": e1}],
        )
    assert "not currently owned" in excinfo.value.issues[0]["problem"]


def test_duplicate_player_within_one_trade_is_rejected():
    _db, season, entries, _players, ownership, _pool, _draft, preseason = domain(entries=2, limit=2)
    preseason.open_window(season.season_id)
    e0, e1 = (entry.season_entry_id for entry in entries)
    p0, _p0b = squad(ownership, e0)

    with pytest.raises(PreseasonTradeValidationError) as excinfo:
        preseason.submit_trade(
            season.season_id,
            [
                {"season_player_id": p0, "from_season_entry_id": e0, "to_season_entry_id": e1},
                {"season_player_id": p0, "from_season_entry_id": e0, "to_season_entry_id": e1},
            ],
        )
    assert any("more than one leg" in issue["problem"] for issue in excinfo.value.issues)


def test_wrong_season_player_is_rejected():
    """A player from an entirely different season's pool must never be
    tradeable, even if its ID happens to be presented alongside this
    season's entries."""
    _db1, other_season, _oe, other_players, _oo, _op, _od, _opre = domain(year=2030, entries=1, limit=1)
    _db2, season, entries, _players, ownership, _pool, _draft, preseason = domain(year=2031, entries=2, limit=1)
    preseason.open_window(season.season_id)
    e0, e1 = (entry.season_entry_id for entry in entries)
    foreign_player_id = other_players[0].season_player_id

    with pytest.raises(PreseasonTradeValidationError) as excinfo:
        preseason.submit_trade(
            season.season_id,
            [{"season_player_id": foreign_player_id, "from_season_entry_id": e0, "to_season_entry_id": e1}],
        )
    assert "player pool" in excinfo.value.issues[0]["problem"]


def test_cross_season_entries_are_rejected():
    _db1, season_a, entries_a, _pa, _oa, _poola, _da, _prea = domain(year=2032, entries=1, limit=1)
    _db2, season_b, entries_b, _players_b, ownership_b, _pool_b, _draft_b, preseason_b = domain(
        year=2033, entries=2, limit=1
    )
    preseason_b.open_window(season_b.season_id)
    p0 = squad(ownership_b, entries_b[0].season_entry_id)[0]
    foreign_entry_id = entries_a[0].season_entry_id

    with pytest.raises(PreseasonTradeValidationError) as excinfo:
        preseason_b.submit_trade(
            season_b.season_id,
            [
                {
                    "season_player_id": p0,
                    "from_season_entry_id": entries_b[0].season_entry_id,
                    "to_season_entry_id": foreign_entry_id,
                }
            ],
        )
    assert "belong to this season" in excinfo.value.issues[0]["problem"]


# -- Squad validation --------------------------------------------------------


def test_valid_squads_close_the_window_and_invalid_squads_block_it_with_diagnostics():
    _db, season, entries, players, ownership, _pool, _draft, preseason = domain(entries=3, limit=2)
    preseason.open_window(season.season_id)
    assert preseason.validate_squads(season.season_id) == []

    broken_entry = entries[0].season_entry_id
    ownership.release(squad(ownership, broken_entry)[0])

    with pytest.raises(PreseasonSquadValidationError) as excinfo:
        preseason.close_window(season.season_id)
    [issue] = excinfo.value.issues
    assert issue["season_entry_id"] == broken_entry
    assert issue["expected_squad_size"] == 2
    assert issue["actual_squad_size"] == 1

    # Nothing changed: window is still open, no snapshot exists.
    assert preseason.get_window(season.season_id).is_open
    assert preseason.current_snapshot(season.season_id) is None


def test_all_ten_squads_must_be_valid_before_closure_succeeds():
    _db, season, entries, players, ownership, _pool, _draft, preseason = domain(entries=10, limit=3, extra_players=1)
    preseason.open_window(season.season_id)
    broken = entries[7].season_entry_id
    ownership.release(squad(ownership, broken)[0])

    with pytest.raises(PreseasonSquadValidationError):
        preseason.close_window(season.season_id)

    # Fix the one broken squad with a free agent and retry -- now every one
    # of the ten squads is valid and closure succeeds.
    ownership.acquire(players[-1].season_player_id, broken)
    window = preseason.close_window(season.season_id)
    assert not window.is_open
    for entry in entries:
        assert len(preseason.opening_squad(season.season_id, entry.season_entry_id)) == 3


# -- Opening snapshot ---------------------------------------------------------


def test_closure_freezes_a_reproducible_stable_opening_snapshot():
    _db, season, entries, _players, ownership, _pool, _draft, preseason = domain(entries=3, limit=2)
    preseason.open_window(season.season_id)
    expected = {entry.season_entry_id: set(squad(ownership, entry.season_entry_id)) for entry in entries}
    preseason.close_window(season.season_id, reason="lock it in")

    for entry in entries:
        frozen = {row.season_player_id for row in preseason.opening_squad(season.season_id, entry.season_entry_id)}
        assert frozen == expected[entry.season_entry_id]

    snapshot = preseason.current_snapshot(season.season_id)
    assert snapshot.version == 1
    # Re-reading is stable and independent of any later, unrelated read.
    assert preseason.current_snapshot(season.season_id) == snapshot


def test_opening_snapshot_authorised_correction_is_attributable_and_history_preserving():
    db, season, entries, players, ownership, _pool, _draft, preseason = domain(entries=2, limit=1, extra_players=1)
    preseason.open_window(season.season_id)
    e0 = entries[0].season_entry_id
    preseason.close_window(season.season_id)
    v1 = preseason.current_snapshot(season.season_id)

    wrong_player = squad(ownership, e0)[0]
    free_agent = players[-1].season_player_id

    from app.audit import ActorContext

    corrected = preseason.correct_opening_snapshot(
        season.season_id,
        e0,
        remove_season_player_id=wrong_player,
        add_season_player_id=free_agent,
        actor=ActorContext.anonymous_operator("admin"),
        reason="data entry error: wrong player frozen",
    )
    assert corrected.version == v1.version + 1
    assert [row.season_player_id for row in preseason.opening_squad(season.season_id, e0)] == [free_agent]

    # The prior version's rows are untouched -- history preserved, not rewritten.
    old_rows = db.execute(
        "SELECT * FROM preseason_opening_snapshot_entry WHERE snapshot_id=? AND season_entry_id=?",
        (v1.snapshot_id, e0),
    ).fetchall()
    assert {row["season_player_id"] for row in old_rows} == {wrong_player}
    assert len(preseason.snapshot_versions(season.season_id)) == 2

    events = AuditEventRepository(db).list_events(action="preseason.correction.applied")
    assert len(events) == 1
    assert events[0].reason == "data entry error: wrong player frozen"
    assert events[0].actor_role == "admin"


def test_correction_requires_reason_and_only_applies_after_closure():
    _db, season, entries, players, ownership, _pool, _draft, preseason = domain(entries=2, limit=1, extra_players=1)
    preseason.open_window(season.season_id)
    e0 = entries[0].season_entry_id
    p0 = squad(ownership, e0)[0]
    free_agent = players[-1].season_player_id

    with pytest.raises(PreseasonStateError, match="closed"):
        preseason.correct_opening_snapshot(
            season.season_id, e0, remove_season_player_id=p0, add_season_player_id=free_agent, reason="too early"
        )

    preseason.close_window(season.season_id)
    with pytest.raises(ValueError, match="explicit reason"):
        preseason.correct_opening_snapshot(
            season.season_id, e0, remove_season_player_id=p0, add_season_player_id=free_agent, reason="  "
        )
    with pytest.raises(PreseasonSnapshotError, match="not part of"):
        preseason.correct_opening_snapshot(
            season.season_id, e0, remove_season_player_id=free_agent, add_season_player_id=p0, reason="wrong target"
        )


# -- Audit ---------------------------------------------------------------


def test_window_lifecycle_events_are_audited():
    db, season, _entries, _players, _ownership, _pool, _draft, preseason = domain()
    preseason.open_window(season.season_id, reason="starting preseason trading")
    preseason.close_window(season.season_id, reason="all squads valid")

    events = AuditEventRepository(db)
    [opened] = events.list_events(action="preseason.window.opened")
    assert opened.reason == "starting preseason trading"
    [closed] = events.list_events(action="preseason.window.closed")
    assert closed.reason == "all squads valid"
    [frozen] = events.list_events(action="preseason.squad.frozen")
    assert frozen.after_state["version"] == 1
