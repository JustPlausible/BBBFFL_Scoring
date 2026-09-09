"""Mid-season draft lifecycle, ladder-order snapshot, delistings, trades,
selection generation and completion (issue #164)."""

import pytest

from app.audit import ActorContext, AuditEventRepository
from app.draft import DraftRepository
from app.ladder import LadderRepository
from app.midseason_draft import (
    MidseasonDraftRepository,
    MidseasonDraftStateError,
    MidseasonPendingTradesError,
    MidseasonPickReconciliationError,
    MidseasonRoundNotFinalError,
    MidseasonTradeValidationError,
    vacancy_allocations,
)
from app.season import SeasonRepository
from tests.midseason_draft_helpers import build_season

ACTOR = ActorContext.anonymous_operator("scorer")


def _setup(**kwargs):
    ctx = build_season(**kwargs)
    SeasonRepository(ctx["database"]).set_midseason_draft_trigger_round(
        ctx["season"].season_id, kwargs.get("trigger_round", 10)
    )
    ctx["midseason"] = MidseasonDraftRepository(ctx["database"])
    return ctx


def _confirmed(**kwargs):
    ctx = _setup(**kwargs)
    ctx["midseason"].confirm_ladder(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR)
    return ctx


def _delisting_open(**kwargs):
    ctx = _confirmed(**kwargs)
    ctx["midseason"].open_delisting_window(ctx["season"].season_id, actor=ACTOR)
    return ctx


# -- 1. Round finality gate ------------------------------------------------


def test_confirm_ladder_rejects_when_final_round_missing():
    ctx = _setup(trigger_round=9, regular_season_round_count=12)
    m, season = ctx["midseason"], ctx["season"]
    SeasonRepository(ctx["database"]).set_midseason_draft_trigger_round(season.season_id, 10)
    with pytest.raises(MidseasonRoundNotFinalError):
        m.confirm_ladder(season.season_id, ctx["competition"].competition_id, actor=ACTOR)


def test_confirm_ladder_requires_configured_trigger_round():
    ctx = build_season(trigger_round=10)
    m = MidseasonDraftRepository(ctx["database"])
    with pytest.raises(MidseasonDraftStateError):
        m.confirm_ladder(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR)


def test_confirm_ladder_refuses_a_second_draft_for_the_same_season():
    ctx = _confirmed(trigger_round=10)
    m, season = ctx["midseason"], ctx["season"]
    with pytest.raises(MidseasonDraftStateError):
        m.confirm_ladder(season.season_id, ctx["competition"].competition_id, actor=ACTOR)


# -- 2/3. Ladder snapshot, draft order, override -----------------------------


def test_draft_order_is_derived_from_the_reverse_ladder_snapshot():
    ctx = _confirmed(trigger_round=10, entry_count=10)
    m, entries = ctx["midseason"], ctx["entries"]
    order = m.draft_order(ctx["season"].season_id)
    assert [entry_id for _, entry_id, source in order] == [entries[i].season_entry_id for i in range(9, -1, -1)]
    assert all(source == "ladder" for _, _, source in order)

    snapshot = m.ladder_snapshot(ctx["season"].season_id)
    assert snapshot.through_round == 10
    assert len(snapshot.rows) == 10
    live_ladder = LadderRepository(ctx["database"]).snapshot(ctx["competition"].competition_id, 10)
    assert {row.season_entry_id: row.rank for row in snapshot.rows} == {
        row.season_entry_id: row.rank for row in live_ladder.rows
    }


def test_override_draft_order_does_not_mutate_the_frozen_ladder_snapshot():
    ctx = _confirmed(trigger_round=10)
    m, entries, season = ctx["midseason"], ctx["entries"], ctx["season"]
    snapshot_before = m.ladder_snapshot(season.season_id)
    live_before = LadderRepository(ctx["database"]).snapshot(ctx["competition"].competition_id, 10)

    swapped = [entries[i].season_entry_id for i in [1, 0, 2, 3, 4, 5, 6, 7, 8, 9]]
    order_before = [e for _, e, _ in m.draft_order(season.season_id)]
    new_order = list(reversed(swapped))  # any explicit, valid permutation
    m.override_draft_order(season.season_id, new_order, actor=ACTOR, reason="competition-determined exception")

    order_after = m.draft_order(season.season_id)
    assert [entry_id for _, entry_id, _ in order_after] == new_order
    assert all(source == "override" for _, _, source in order_after)
    assert [entry_id for _, entry_id, _ in order_after] != order_before

    snapshot_after = m.ladder_snapshot(season.season_id)
    live_after = LadderRepository(ctx["database"]).snapshot(ctx["competition"].competition_id, 10)
    assert snapshot_after == snapshot_before
    assert live_after == live_before

    events = AuditEventRepository(ctx["database"]).list_events(action="midseason.draft_order.overridden")
    assert len(events) == 1
    assert events[0].reason == "competition-determined exception"


def test_override_draft_order_requires_a_reason_and_exact_entry_set():
    ctx = _confirmed(trigger_round=10)
    m, entries, season = ctx["midseason"], ctx["entries"], ctx["season"]
    with pytest.raises(ValueError):
        m.override_draft_order(season.season_id, [e.season_entry_id for e in entries], actor=ACTOR, reason="")
    with pytest.raises(MidseasonDraftStateError):
        m.override_draft_order(
            season.season_id, [e.season_entry_id for e in entries[:9]], actor=ACTOR, reason="missing an entry"
        )


def test_override_draft_order_blocked_once_delistings_are_locked():
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    m.lock_delistings(season.season_id, actor=ACTOR)
    with pytest.raises(MidseasonDraftStateError):
        m.override_draft_order(
            season.season_id, [e.season_entry_id for e in reversed(entries)], actor=ACTOR, reason="too late"
        )


# -- 4/5. Delistings: submit/withdraw before lock, immutable after --------


def test_delisting_can_be_submitted_withdrawn_and_resubmitted_before_lock():
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst = entries[9]
    squad = m.ownership.current_squad(worst.season_entry_id)
    player_id = squad[0].season_player_id

    delisting = m.submit_delisting(season.season_id, worst.season_entry_id, player_id, actor=ACTOR, reason="cutting")
    assert delisting.withdrawn_at is None

    withdrawn = m.withdraw_delisting(season.season_id, delisting.delisting_id, actor=ACTOR, reason="trade opportunity")
    assert withdrawn.withdrawn_at is not None

    resubmitted = m.submit_delisting(season.season_id, worst.season_entry_id, player_id, actor=ACTOR)
    assert resubmitted.delisting_id != delisting.delisting_id
    assert resubmitted.withdrawn_at is None

    active = m.list_delistings(season.season_id, include_withdrawn=False)
    assert [item.delisting_id for item in active] == [resubmitted.delisting_id]

    events = AuditEventRepository(ctx["database"]).list_events(
        entity_type="midseason.delisting", entity_id=delisting.delisting_id
    )
    assert [event.action for event in events] == ["midseason.delisting.submitted", "midseason.delisting.withdrawn"]


def test_submit_delisting_rejects_a_player_not_owned_by_that_entry():
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    other_squad = m.ownership.current_squad(entries[8].season_entry_id)
    with pytest.raises(MidseasonDraftStateError):
        m.submit_delisting(season.season_id, entries[9].season_entry_id, other_squad[0].season_player_id, actor=ACTOR)


def test_submit_delisting_rejects_duplicate_active_delisting_of_the_same_player():
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    squad = m.ownership.current_squad(entries[9].season_entry_id)
    m.submit_delisting(season.season_id, entries[9].season_entry_id, squad[0].season_player_id, actor=ACTOR)
    with pytest.raises(MidseasonDraftStateError):
        m.submit_delisting(season.season_id, entries[9].season_entry_id, squad[0].season_player_id, actor=ACTOR)


def test_delistings_cannot_be_submitted_or_withdrawn_before_the_window_opens_or_after_it_locks():
    ctx = _confirmed(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    squad = m.ownership.current_squad(entries[9].season_entry_id)
    with pytest.raises(MidseasonDraftStateError):
        m.submit_delisting(season.season_id, entries[9].season_entry_id, squad[0].season_player_id, actor=ACTOR)

    m.open_delisting_window(season.season_id, actor=ACTOR)
    delisting = m.submit_delisting(season.season_id, entries[9].season_entry_id, squad[0].season_player_id, actor=ACTOR)
    m.lock_delistings(season.season_id, actor=ACTOR)

    with pytest.raises(MidseasonDraftStateError):
        m.withdraw_delisting(season.season_id, delisting.delisting_id, actor=ACTOR)
    with pytest.raises(MidseasonDraftStateError):
        m.submit_delisting(season.season_id, entries[9].season_entry_id, squad[1].season_player_id, actor=ACTOR)

    # Locked delistings are not silently modifiable: the row itself now
    # carries a locked_at timestamp set only by the lock action.
    locked = m.get_delisting(delisting.delisting_id)
    assert locked.locked_at is not None
    assert locked.withdrawn_at is None


# -- 6/7/8. Trades: pending blocks lock, proposal alone never changes -----
# ownership, approval does (and is audited) --------------------------------


def test_propose_trade_does_not_change_ownership():
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    best, second = entries[0], entries[1]
    best_before = {p.season_player_id for p in m.ownership.current_squad(best.season_entry_id)}
    second_before = {p.season_player_id for p in m.ownership.current_squad(second.season_entry_id)}
    player = next(iter(best_before))

    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": best.season_entry_id,
                "to_season_entry_id": second.season_entry_id,
                "season_player_id": player,
            }
        ],
        actor=ACTOR,
    )
    assert trade.status == "pending"
    assert {p.season_player_id for p in m.ownership.current_squad(best.season_entry_id)} == best_before
    assert {p.season_player_id for p in m.ownership.current_squad(second.season_entry_id)} == second_before


def test_lock_delistings_is_blocked_while_a_trade_is_pending():
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    best, second = entries[0], entries[1]
    player = next(iter(m.ownership.current_squad(best.season_entry_id))).season_player_id
    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": best.season_entry_id,
                "to_season_entry_id": second.season_entry_id,
                "season_player_id": player,
            }
        ],
        actor=ACTOR,
    )
    with pytest.raises(MidseasonPendingTradesError) as excinfo:
        m.lock_delistings(season.season_id, actor=ACTOR)
    assert excinfo.value.trade_ids == [trade.trade_id]

    m.decide_trade(season.season_id, trade.trade_id, False, actor=ACTOR, reason="withdrawn by agreement")
    # Now unblocked -- no pending trades remain.
    m.lock_delistings(season.season_id, actor=ACTOR)


def test_approved_player_trade_updates_ownership_immediately_and_is_audited():
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    best, second = entries[0], entries[1]
    best_player = next(iter(m.ownership.current_squad(best.season_entry_id))).season_player_id
    second_player = next(iter(m.ownership.current_squad(second.season_entry_id))).season_player_id

    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": best.season_entry_id,
                "to_season_entry_id": second.season_entry_id,
                "season_player_id": best_player,
            },
            {
                "leg_type": "player",
                "from_season_entry_id": second.season_entry_id,
                "to_season_entry_id": best.season_entry_id,
                "season_player_id": second_player,
            },
        ],
        actor=ACTOR,
    )
    decided = m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="approved by scorer")
    assert decided.status == "approved"

    assert best_player in {p.season_player_id for p in m.ownership.current_squad(second.season_entry_id)}
    assert second_player in {p.season_player_id for p in m.ownership.current_squad(best.season_entry_id)}

    events = AuditEventRepository(ctx["database"]).list_events(action="midseason.trade.approved")
    assert len(events) == 1 and events[0].reason == "approved by scorer"
    acquire_events = AuditEventRepository(ctx["database"]).list_events(
        correlation_id=trade.correlation_id, action="ownership.player.acquired"
    )
    assert len(acquire_events) == 2


def test_rejected_trade_never_changes_ownership_and_cannot_be_decided_twice():
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    best, second = entries[0], entries[1]
    player = next(iter(m.ownership.current_squad(best.season_entry_id))).season_player_id
    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": best.season_entry_id,
                "to_season_entry_id": second.season_entry_id,
                "season_player_id": player,
            }
        ],
        actor=ACTOR,
    )
    m.decide_trade(season.season_id, trade.trade_id, False, actor=ACTOR, reason="declined")
    assert player in {p.season_player_id for p in m.ownership.current_squad(best.season_entry_id)}
    with pytest.raises(MidseasonDraftStateError):
        m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="too late")


def test_propose_trade_rejects_invalid_legs_and_writes_nothing():
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    with pytest.raises(MidseasonTradeValidationError) as excinfo:
        m.propose_trade(
            season.season_id,
            [
                {
                    "leg_type": "player",
                    "from_season_entry_id": entries[0].season_entry_id,
                    "to_season_entry_id": entries[0].season_entry_id,
                    "season_player_id": "whatever",
                }
            ],
            actor=ACTOR,
        )
    assert excinfo.value.issues
    assert m.list_trades(season.season_id) == []


def test_pick_leg_trade_rejected_once_delistings_are_locked():
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    m.lock_delistings(season.season_id, actor=ACTOR)
    with pytest.raises(MidseasonDraftStateError):
        m.propose_trade(
            season.season_id,
            [
                {
                    "leg_type": "pick",
                    "from_season_entry_id": entries[9].season_entry_id,
                    "to_season_entry_id": entries[0].season_entry_id,
                    "draft_round": 1,
                }
            ],
            actor=ACTOR,
        )


def test_confirm_ladder_rejects_a_competition_from_a_different_season():
    ctx = _setup(trigger_round=10)
    other = build_season(trigger_round=10)
    m, season = ctx["midseason"], ctx["season"]
    with pytest.raises(MidseasonDraftStateError):
        m.confirm_ladder(season.season_id, other["competition"].competition_id, actor=ACTOR)


def test_decide_trade_rejects_a_stale_leg_whose_ownership_moved_since_proposal():
    """Codex review: two approved-in-sequence trades must not let the
    second one release a player from whoever the first one already moved
    it to, based on a stale `from_season_entry_id`."""
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    a, b, c = entries[0], entries[1], entries[2]
    player = next(iter(m.ownership.current_squad(a.season_entry_id))).season_player_id
    b_player = next(iter(m.ownership.current_squad(b.season_entry_id))).season_player_id
    c_player = next(iter(m.ownership.current_squad(c.season_entry_id))).season_player_id

    trade_to_b = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": a.season_entry_id,
                "to_season_entry_id": b.season_entry_id,
                "season_player_id": player,
            },
            {
                "leg_type": "player",
                "from_season_entry_id": b.season_entry_id,
                "to_season_entry_id": a.season_entry_id,
                "season_player_id": b_player,
            },
        ],
        actor=ACTOR,
    )
    stale_trade_from_a = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": a.season_entry_id,
                "to_season_entry_id": c.season_entry_id,
                "season_player_id": player,
            },
            {
                "leg_type": "player",
                "from_season_entry_id": c.season_entry_id,
                "to_season_entry_id": a.season_entry_id,
                "season_player_id": c_player,
            },
        ],
        actor=ACTOR,
    )

    m.decide_trade(season.season_id, trade_to_b.trade_id, True, actor=ACTOR, reason="approved first")
    assert player in {p.season_player_id for p in m.ownership.current_squad(b.season_entry_id)}

    with pytest.raises(MidseasonDraftStateError):
        m.decide_trade(season.season_id, stale_trade_from_a.trade_id, True, actor=ACTOR, reason="stale, must refuse")

    # Ownership is unchanged by the refused decision: still with b, not c.
    assert player in {p.season_player_id for p in m.ownership.current_squad(b.season_entry_id)}
    assert player not in {p.season_player_id for p in m.ownership.current_squad(c.season_entry_id)}


def test_approving_a_player_trade_auto_withdraws_the_players_active_delisting():
    """Codex review: a stale delisting recorded against the player's
    previous owner must not survive an approved trade that moves the
    player on, or `lock_delistings` would release it from the new owner
    based on the old owner's declaration."""
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    a, b = entries[0], entries[1]
    player = next(iter(m.ownership.current_squad(a.season_entry_id))).season_player_id
    b_player = next(iter(m.ownership.current_squad(b.season_entry_id))).season_player_id

    delisting = m.submit_delisting(
        season.season_id, a.season_entry_id, player, actor=ACTOR, reason="considering cutting"
    )
    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": a.season_entry_id,
                "to_season_entry_id": b.season_entry_id,
                "season_player_id": player,
            },
            {
                "leg_type": "player",
                "from_season_entry_id": b.season_entry_id,
                "to_season_entry_id": a.season_entry_id,
                "season_player_id": b_player,
            },
        ],
        actor=ACTOR,
    )
    m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="approved")

    reloaded = m.get_delisting(delisting.delisting_id)
    assert reloaded.withdrawn_at is not None

    m.lock_delistings(season.season_id, actor=ACTOR)
    # The player now belongs to b and must not have been released by a's
    # stale delisting.
    assert player in {p.season_player_id for p in m.ownership.current_squad(b.season_entry_id)}


def test_decide_trade_tolerates_a_temporary_squad_capacity_overage_for_a_player_for_pick_trade():
    """Codex review: a documented player-for-pick trade can legitimately
    approve into a squad that is still showing full, because that entry's
    own matching delisting has not been released yet -- release only
    happens later, atomically, at `lock_delistings`. Without
    `allow_capacity_overage` on the player-leg acquisition, `decide_trade`
    would refuse the exact trade shape issue #164 requires, even though the
    imbalance is only ever temporary and self-resolves at lock time."""
    ctx = _delisting_open(trigger_round=10, squad_limit=4)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    receiving, giving = entries[0], entries[1]
    assert len(m.ownership.current_squad(receiving.season_entry_id)) == 4
    outgoing_player = next(iter(m.ownership.current_squad(receiving.season_entry_id))).season_player_id
    m.submit_delisting(season.season_id, receiving.season_entry_id, outgoing_player, actor=ACTOR)

    incoming_player = next(iter(m.ownership.current_squad(giving.season_entry_id))).season_player_id
    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": giving.season_entry_id,
                "to_season_entry_id": receiving.season_entry_id,
                "season_player_id": incoming_player,
            },
            {
                "leg_type": "pick",
                "from_season_entry_id": receiving.season_entry_id,
                "to_season_entry_id": giving.season_entry_id,
                "draft_round": 1,
            },
        ],
        actor=ACTOR,
    )
    # Would raise SquadCapacityError without allow_capacity_overage:
    # receiving's delisted player has not been released yet, so its squad
    # still shows all 4 slots full.
    m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="player-for-pick")

    receiving_squad = {p.season_player_id for p in m.ownership.current_squad(receiving.season_entry_id)}
    assert len(receiving_squad) == 5
    assert incoming_player in receiving_squad

    m.lock_delistings(season.season_id, actor=ACTOR)
    # The pending delisting releases at lock time, restoring receiving to
    # exactly its configured squad limit -- the temporary overage was only
    # ever transient.
    assert len(m.ownership.current_squad(receiving.season_entry_id)) == 4
    m.generate_selection_table(season.season_id, actor=ACTOR)


def test_decide_trade_is_blocked_once_post_draft_trading_is_closed():
    ctx = _delisting_open(trigger_round=10, squad_limit=4)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst = entries[9]
    squad = m.ownership.current_squad(worst.season_entry_id)
    m.submit_delisting(season.season_id, worst.season_entry_id, squad[0].season_player_id, actor=ACTOR)
    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)
    pool = list(m.available_player_pool(season.season_id))
    while True:
        nxt = m.next_pick(season.season_id)
        if nxt is None:
            break
        m.execute_pick(season.season_id, nxt.current_season_entry_id, pool.pop(0).season_player_id, actor=ACTOR)
    assert m.get_draft(season.season_id).state == "draft_complete"

    a, b = entries[0], entries[1]
    player = next(iter(m.ownership.current_squad(a.season_entry_id))).season_player_id
    b_player = next(iter(m.ownership.current_squad(b.season_entry_id))).season_player_id
    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "player",
                "from_season_entry_id": a.season_entry_id,
                "to_season_entry_id": b.season_entry_id,
                "season_player_id": player,
            },
            {
                "leg_type": "player",
                "from_season_entry_id": b.season_entry_id,
                "to_season_entry_id": a.season_entry_id,
                "season_player_id": b_player,
            },
        ],
        actor=ACTOR,
    )

    m.close_post_draft_trading(season.season_id, actor=ACTOR, reason="Round 11 lockout")
    with pytest.raises(MidseasonDraftStateError):
        m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="too late")
    # Ownership is unaffected by the refused decision.
    assert player in {p.season_player_id for p in m.ownership.current_squad(a.season_entry_id)}


def test_reopen_draft_refused_once_post_draft_trading_is_closed():
    ctx = _delisting_open(trigger_round=10, squad_limit=4)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst = entries[9]
    squad = m.ownership.current_squad(worst.season_entry_id)
    m.submit_delisting(season.season_id, worst.season_entry_id, squad[0].season_player_id, actor=ACTOR)
    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)
    pool = list(m.available_player_pool(season.season_id))
    while True:
        nxt = m.next_pick(season.season_id)
        if nxt is None:
            break
        m.execute_pick(season.season_id, nxt.current_season_entry_id, pool.pop(0).season_player_id, actor=ACTOR)
    m.close_post_draft_trading(season.season_id, actor=ACTOR, reason="Round 11 lockout")

    with pytest.raises(MidseasonDraftStateError):
        m.reopen_draft(season.season_id, actor=ACTOR, reason="too late, already complete")
    # The engine draft must remain finalized -- the guard fires before the
    # engine is ever touched.
    assert m.status(season.season_id).is_finalized


def test_same_round_pick_swap_correctly_exchanges_both_slots():
    """Codex review: applying same-round two-way pick-trade legs by
    overwriting a shared (round, current_owner) lookup silently loses one
    side of the swap. Both entries need a vacancy in the same round for
    each to have its own round-1 slot to swap."""
    ctx = _delisting_open(trigger_round=10, squad_limit=4)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst, second_worst = entries[9], entries[8]
    worst_squad = m.ownership.current_squad(worst.season_entry_id)
    second_squad = m.ownership.current_squad(second_worst.season_entry_id)
    m.submit_delisting(season.season_id, worst.season_entry_id, worst_squad[0].season_player_id, actor=ACTOR)
    m.submit_delisting(season.season_id, second_worst.season_entry_id, second_squad[0].season_player_id, actor=ACTOR)

    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "pick",
                "from_season_entry_id": worst.season_entry_id,
                "to_season_entry_id": second_worst.season_entry_id,
                "draft_round": 1,
            },
            {
                "leg_type": "pick",
                "from_season_entry_id": second_worst.season_entry_id,
                "to_season_entry_id": worst.season_entry_id,
                "draft_round": 1,
            },
        ],
        actor=ACTOR,
    )
    m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="same-round swap")
    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)

    picks = m.picks(season.season_id)
    assert len(picks) == 2
    by_original = {p.original_season_entry_id: p.current_season_entry_id for p in picks}
    assert by_original[worst.season_entry_id] == second_worst.season_entry_id
    assert by_original[second_worst.season_entry_id] == worst.season_entry_id


# -- 9/10/11/12. Generate selections, pool eligibility, ownership -----------


def test_generate_selection_table_rejected_before_lock():
    ctx = _delisting_open(trigger_round=10)
    m, season = ctx["midseason"], ctx["season"]
    with pytest.raises(MidseasonDraftStateError):
        m.generate_selection_table(season.season_id, actor=ACTOR)


def test_available_pool_includes_locked_delisted_and_previously_undrafted_eligible_players_only():
    ctx = _delisting_open(trigger_round=10, squad_limit=4)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst = entries[9]
    squad = m.ownership.current_squad(worst.season_entry_id)
    delisting = m.submit_delisting(season.season_id, worst.season_entry_id, squad[0].season_player_id, actor=ACTOR)

    # A brand-new, never-owned eligible player -- "previously undrafted".
    fresh = m.player_pool.refresh_player(season.season_id, 5_000_001, "Fresh eligible player")
    # An ineligible player must never appear in the pool.
    ineligible = m.player_pool.refresh_player(season.season_id, 5_000_002, "Injured/unresolved player", eligible=False)

    pool_before_lock = {p.season_player_id for p in m.available_player_pool(season.season_id)}
    assert fresh.season_player_id in pool_before_lock
    assert delisting.season_player_id not in pool_before_lock  # still owned; lock has not released it
    assert ineligible.season_player_id not in pool_before_lock

    m.lock_delistings(season.season_id, actor=ACTOR)
    pool_after_lock = {p.season_player_id for p in m.available_player_pool(season.season_id)}
    assert delisting.season_player_id in pool_after_lock
    assert fresh.season_player_id in pool_after_lock
    assert ineligible.season_player_id not in pool_after_lock

    # Any player still owned by another squad must never be in the pool.
    still_owned = next(iter(m.ownership.current_squad(entries[0].season_entry_id))).season_player_id
    assert still_owned not in pool_after_lock


def test_generate_selection_table_produces_correct_vacancy_based_numbered_picks():
    ctx = _delisting_open(trigger_round=10, squad_limit=3)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    # Only the two worst teams delist anyone; every other team keeps a full
    # squad (zero vacancies) and is skipped by the allocation entirely.
    worst, second_worst = entries[9], entries[8]
    worst_squad = m.ownership.current_squad(worst.season_entry_id)
    second_squad = m.ownership.current_squad(second_worst.season_entry_id)
    m.submit_delisting(season.season_id, worst.season_entry_id, worst_squad[0].season_player_id, actor=ACTOR)
    m.submit_delisting(season.season_id, worst.season_entry_id, worst_squad[1].season_player_id, actor=ACTOR)
    m.submit_delisting(season.season_id, second_worst.season_entry_id, second_squad[0].season_player_id, actor=ACTOR)

    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)

    picks = m.picks(season.season_id)
    # Reverse-ladder order puts entries[9] before entries[8] before anyone
    # else; entries[9] has 2 vacancies, entries[8] has 1: round 1 has both,
    # round 2 only entries[9]. No other team appears at all.
    assert [(p.draft_round, p.round_position, p.original_season_entry_id) for p in picks] == [
        (1, 1, worst.season_entry_id),
        (1, 2, second_worst.season_entry_id),
        (2, 1, worst.season_entry_id),
    ]
    assert [p.overall_number for p in picks] == [1, 2, 3]

    status = m.status(season.season_id)
    assert status.total_picks == 3 and status.completed_picks == 0 and status.target_squad_size == 3


def test_generate_selection_table_refuses_when_no_team_has_a_vacancy():
    ctx = _delisting_open(trigger_round=10)
    m, season = ctx["midseason"], ctx["season"]
    m.lock_delistings(season.season_id, actor=ACTOR)
    with pytest.raises(MidseasonDraftStateError):
        m.generate_selection_table(season.season_id, actor=ACTOR)


def test_lock_delistings_refuses_a_pick_trade_that_cannot_reconcile_and_recovers_after_a_compensating_trade():
    """Codex review: an approved round-based pick trade can hand an entry
    more selections than it has roster vacancies for -- and, on the other
    side of that same trade, leave the entry that gave the pick away with
    fewer selections than its own vacancies (pick ownership is a
    downstream, generation-time concept -- `propose_trade`/`decide_trade`
    cannot see the eventual allocation, so neither side is individually
    invalid at approval time). Left unchecked this would only surface
    later as a pick nobody can legally execute (or a roster slot that can
    never be filled), permanently stranding the draft. `lock_delistings`
    must instead catch this before it ever commits, roll back cleanly (the
    delisting window stays open, nothing already released is lost), and
    leave the Scorer a real way out: a compensating trade, then retry the
    lock."""
    ctx = _delisting_open(trigger_round=10, squad_limit=4)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst, best = entries[9], entries[0]
    worst_squad = m.ownership.current_squad(worst.season_entry_id)
    best_squad = m.ownership.current_squad(best.season_entry_id)
    m.submit_delisting(season.season_id, worst.season_entry_id, worst_squad[0].season_player_id, actor=ACTOR)
    m.submit_delisting(season.season_id, worst.season_entry_id, worst_squad[1].season_player_id, actor=ACTOR)
    m.submit_delisting(season.season_id, best.season_entry_id, best_squad[0].season_player_id, actor=ACTOR)
    m.submit_delisting(season.season_id, best.season_entry_id, best_squad[1].season_player_id, actor=ACTOR)
    # Both `worst` and `best` now have 2 vacancies each (own round-1 and
    # round-2 picks).

    # A pick-for-nothing trade: best gains worst's round-1 pick without
    # shedding anything -- valid to *propose and approve* (ownership of
    # picks is a downstream, generation-time concept), but it leaves best
    # with 3 selections against 2 vacancies, and worst with only 1 against
    # its own 2.
    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "pick",
                "from_season_entry_id": worst.season_entry_id,
                "to_season_entry_id": best.season_entry_id,
                "draft_round": 1,
            }
        ],
        actor=ACTOR,
    )
    m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="approved")

    with pytest.raises(MidseasonPickReconciliationError) as excinfo:
        m.lock_delistings(season.season_id, actor=ACTOR)
    assert excinfo.value.mismatched == {
        worst.season_entry_id: {"allocated": 1, "vacancies": 2},
        best.season_entry_id: {"allocated": 3, "vacancies": 2},
    }

    # Nothing committed: the delisting window is still open, and no
    # delisted player was released by the failed attempt.
    draft = m.get_draft(season.season_id)
    assert draft.state == "delisting_open"
    assert all(delisting.locked_at is None for delisting in m.list_delistings(season.season_id))

    # Recovery: `best` trades its own round-2 pick back to `worst` -- a
    # different round from the original mistake's round-1 leg, so this
    # resolves independently rather than chaining through the same
    # (round, entry) redirect. That exactly restores both entries to their
    # own vacancy count: `best` ends up with worst's round-1 pick plus its
    # own round-1 pick (2, matching its 2 vacancies); `worst` ends up with
    # its own round-2 pick plus best's round-2 pick (2, matching its own).
    compensating = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "pick",
                "from_season_entry_id": best.season_entry_id,
                "to_season_entry_id": worst.season_entry_id,
                "draft_round": 2,
            }
        ],
        actor=ACTOR,
    )
    m.decide_trade(season.season_id, compensating.trade_id, True, actor=ACTOR, reason="rebalance")

    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)
    picks = m.picks(season.season_id)
    round1_from_worst = [
        p for p in picks if p.draft_round == 1 and p.original_season_entry_id == worst.season_entry_id
    ][0]
    round2_from_best = [p for p in picks if p.draft_round == 2 and p.original_season_entry_id == best.season_entry_id][
        0
    ]
    assert round1_from_worst.current_season_entry_id == best.season_entry_id
    assert round2_from_best.current_season_entry_id == worst.season_entry_id

    while True:
        nxt = m.next_pick(season.season_id)
        if nxt is None:
            break
        pool = m.available_player_pool(season.season_id)
        m.execute_pick(season.season_id, nxt.current_season_entry_id, pool[0].season_player_id, actor=ACTOR)

    for entry in (worst, best):
        assert len(m.ownership.current_squad(entry.season_entry_id)) == 4


def test_pick_for_pick_swap_trade_keeps_every_team_at_its_own_vacancy_count():
    ctx = _delisting_open(trigger_round=10, squad_limit=4)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst, best = entries[9], entries[0]
    worst_squad = m.ownership.current_squad(worst.season_entry_id)
    best_squad = m.ownership.current_squad(best.season_entry_id)
    m.submit_delisting(season.season_id, worst.season_entry_id, worst_squad[0].season_player_id, actor=ACTOR)
    m.submit_delisting(season.season_id, worst.season_entry_id, worst_squad[1].season_player_id, actor=ACTOR)
    m.submit_delisting(season.season_id, best.season_entry_id, best_squad[0].season_player_id, actor=ACTOR)
    m.submit_delisting(season.season_id, best.season_entry_id, best_squad[1].season_player_id, actor=ACTOR)

    trade = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "pick",
                "from_season_entry_id": worst.season_entry_id,
                "to_season_entry_id": best.season_entry_id,
                "draft_round": 1,
            },
            {
                "leg_type": "pick",
                "from_season_entry_id": best.season_entry_id,
                "to_season_entry_id": worst.season_entry_id,
                "draft_round": 2,
            },
        ],
        actor=ACTOR,
    )
    m.decide_trade(season.season_id, trade.trade_id, True, actor=ACTOR, reason="swap")
    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)

    pool = list(m.available_player_pool(season.season_id))
    assert len(pool) == 4
    while True:
        nxt = m.next_pick(season.season_id)
        if nxt is None:
            break
        m.execute_pick(season.season_id, nxt.current_season_entry_id, pool.pop(0).season_player_id, actor=ACTOR)

    for entry in entries:
        assert len(m.ownership.current_squad(entry.season_entry_id)) == 4


def test_decide_trade_rejects_a_second_approval_of_an_already_traded_pick_entitlement():
    """Codex review: two separately-proposed trades that both sell entry
    A's round-1 pick must not both be approved -- the second approval
    would otherwise silently discard the first's already-audited effect at
    generation time."""
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    a, b, c = entries[9], entries[8], entries[7]

    trade_to_b = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "pick",
                "from_season_entry_id": a.season_entry_id,
                "to_season_entry_id": b.season_entry_id,
                "draft_round": 1,
            }
        ],
        actor=ACTOR,
    )
    trade_to_c = m.propose_trade(
        season.season_id,
        [
            {
                "leg_type": "pick",
                "from_season_entry_id": a.season_entry_id,
                "to_season_entry_id": c.season_entry_id,
                "draft_round": 1,
            }
        ],
        actor=ACTOR,
    )
    m.decide_trade(season.season_id, trade_to_b.trade_id, True, actor=ACTOR, reason="approved first")

    with pytest.raises(MidseasonDraftStateError):
        m.decide_trade(season.season_id, trade_to_c.trade_id, True, actor=ACTOR, reason="conflicting, must refuse")

    assert m.get_trade(trade_to_c.trade_id).status == "pending"
    # Rejecting the conflicting proposal instead is still fine.
    m.decide_trade(season.season_id, trade_to_c.trade_id, False, actor=ACTOR, reason="withdrawn by agreement")


def test_propose_trade_rejects_a_duplicate_pick_entitlement_within_the_same_proposal():
    """Codex review: the cross-trade duplicate-entitlement check in
    `decide_trade` only ever looks at *other* already-approved trades --
    it never catches a single proposal that sells the same
    (round, from_entity) pick entitlement to two different legs of
    itself. Left unchecked, the redirect that eventually applies would
    silently depend on dict/iteration order rather than being rejected up
    front. `propose_trade` must catch this before anything is written."""
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    a, b, c = entries[9], entries[8], entries[7]

    with pytest.raises(MidseasonTradeValidationError) as excinfo:
        m.propose_trade(
            season.season_id,
            [
                {
                    "leg_type": "pick",
                    "from_season_entry_id": a.season_entry_id,
                    "to_season_entry_id": b.season_entry_id,
                    "draft_round": 1,
                },
                {
                    "leg_type": "pick",
                    "from_season_entry_id": a.season_entry_id,
                    "to_season_entry_id": c.season_entry_id,
                    "draft_round": 1,
                },
            ],
            actor=ACTOR,
        )
    assert excinfo.value.issues and excinfo.value.issues[0]["leg"] == 1
    assert m.list_trades(season.season_id) == []


def test_reconcile_completion_is_publicly_retryable_and_idempotent():
    """Codex review: recovery from an interruption between the final
    pick's own commit and finalising/transitioning the draft needs its own
    separately callable, safely-repeatable entry point -- `execute_pick`
    cannot itself be retried once the final pick is already completed."""
    ctx = _delisting_open(trigger_round=10, squad_limit=4)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst = entries[9]
    squad = m.ownership.current_squad(worst.season_entry_id)
    m.submit_delisting(season.season_id, worst.season_entry_id, squad[0].season_player_id, actor=ACTOR)
    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)
    pool = list(m.available_player_pool(season.season_id))

    # Complete the final pick via the raw engine call, bypassing
    # execute_pick's own reconciliation step entirely -- simulating a crash
    # right after the pick commits but before reconciliation runs.
    nxt = m.next_pick(season.season_id)
    m.drafts.execute_pick(
        season.season_id, nxt.current_season_entry_id, pool[0].season_player_id, draft_kind="midseason", actor=ACTOR
    )
    assert m.get_draft(season.season_id).state == "draft_open"
    assert not m.status(season.season_id).is_finalized

    m.reconcile_completion(season.season_id, actor=ACTOR)
    assert m.get_draft(season.season_id).state == "draft_complete"
    assert m.status(season.season_id).is_finalized

    # Calling it again is a safe no-op.
    m.reconcile_completion(season.season_id, actor=ACTOR)
    assert m.get_draft(season.season_id).state == "draft_complete"


# -- 13/15. Squad-size validation, automatic completion, Round 11 ---------


def test_full_lifecycle_completes_automatically_and_every_squad_reconciles_to_configured_size():
    ctx = _delisting_open(trigger_round=10, entry_count=10, squad_limit=4, regular_season_round_count=12)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst = entries[9]
    squad = m.ownership.current_squad(worst.season_entry_id)
    m.submit_delisting(season.season_id, worst.season_entry_id, squad[0].season_player_id, actor=ACTOR)
    m.submit_delisting(season.season_id, worst.season_entry_id, squad[1].season_player_id, actor=ACTOR)

    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)

    pool = list(m.available_player_pool(season.season_id))
    assert len(pool) == 2
    while True:
        nxt = m.next_pick(season.season_id)
        if nxt is None:
            break
        m.execute_pick(season.season_id, nxt.current_season_entry_id, pool.pop(0).season_player_id, actor=ACTOR)

    draft = m.get_draft(season.season_id)
    assert draft.state == "draft_complete"
    for entry in entries:
        assert len(m.ownership.current_squad(entry.season_entry_id)) == 4

    events = AuditEventRepository(ctx["database"]).list_events(action="midseason.draft.completed")
    assert len(events) == 1

    completed = m.close_post_draft_trading(season.season_id, actor=ACTOR, reason="Round 11 lockout approaching")
    assert completed.state == "complete"

    # Round 11 proceeds from the same authoritative ownership ledger --
    # bootstrap round 11 and confirm the new squads are what a coach would
    # see there. No further mid-season-draft-specific action is required.
    round_11 = ctx["logical_rounds"][11] if 11 in ctx["logical_rounds"] else None
    assert round_11 is not None
    ordinary = ctx["lifecycle"].create_ordinary_round(round_11.bbbffl_round_id)
    assert ordinary.state == "upcoming"
    for entry in entries:
        assert len(m.ownership.current_squad(entry.season_entry_id)) == 4


def test_finalize_honours_configured_squad_size_not_a_hardcoded_assumption():
    ctx = _delisting_open(trigger_round=10, squad_limit=7)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst = entries[9]
    squad = m.ownership.current_squad(worst.season_entry_id)
    m.submit_delisting(season.season_id, worst.season_entry_id, squad[0].season_player_id, actor=ACTOR)
    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)
    status = m.status(season.season_id)
    assert status.target_squad_size == 7


# -- 14. Exceptional audited corrections ------------------------------------


def test_correct_selection_undoes_the_most_recent_pick_and_is_audited():
    ctx = _delisting_open(trigger_round=10, squad_limit=3)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst = entries[9]
    squad = m.ownership.current_squad(worst.season_entry_id)
    # Two vacancies, so completing the first pick does not yet auto-finalise
    # the draft -- correction must remain possible on the still-active draft.
    m.submit_delisting(season.season_id, worst.season_entry_id, squad[0].season_player_id, actor=ACTOR)
    m.submit_delisting(season.season_id, worst.season_entry_id, squad[1].season_player_id, actor=ACTOR)
    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)

    pool = list(m.available_player_pool(season.season_id))
    nxt = m.next_pick(season.season_id)
    completed = m.execute_pick(season.season_id, nxt.current_season_entry_id, pool[0].season_player_id, actor=ACTOR)

    corrected = m.correct_selection(
        season.season_id, completed.draft_pick_id, actor=ACTOR, reason="wrong player recorded"
    )
    assert corrected.completed_at is None
    assert pool[0].season_player_id not in {
        p.season_player_id for p in m.ownership.current_squad(worst.season_entry_id)
    }

    events = AuditEventRepository(ctx["database"]).list_events(action="draft.pick.corrected")
    assert len(events) == 1 and events[0].reason == "wrong player recorded"


def test_reopen_completed_draft_for_an_exceptional_correction_is_audited():
    ctx = _delisting_open(trigger_round=10, squad_limit=3)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    worst = entries[9]
    squad = m.ownership.current_squad(worst.season_entry_id)
    m.submit_delisting(season.season_id, worst.season_entry_id, squad[0].season_player_id, actor=ACTOR)
    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)
    pool = list(m.available_player_pool(season.season_id))
    nxt = m.next_pick(season.season_id)
    m.execute_pick(season.season_id, nxt.current_season_entry_id, pool[0].season_player_id, actor=ACTOR)

    draft = m.get_draft(season.season_id)
    assert draft.state == "draft_complete"

    reopened = m.reopen_draft(season.season_id, actor=ACTOR, reason="competition agreed correction")
    assert reopened.state == "draft_open"

    events = AuditEventRepository(ctx["database"]).list_events(action="midseason.draft.reopened")
    assert len(events) == 1 and events[0].reason == "competition agreed correction"


# -- 16. Replay/proxy actor provenance --------------------------------------


def test_scorer_proxy_actions_are_recorded_with_anonymous_operator_provenance():
    ctx = _delisting_open(trigger_round=10)
    m, season, entries = ctx["midseason"], ctx["season"], ctx["entries"]
    proxy_actor = ActorContext.anonymous_operator("replay_operator")
    worst = entries[9]
    squad = m.ownership.current_squad(worst.season_entry_id)
    delisting = m.submit_delisting(
        season.season_id,
        worst.season_entry_id,
        squad[0].season_player_id,
        actor=proxy_actor,
        reason="proxy entry for 2026 replay",
    )
    event = AuditEventRepository(ctx["database"]).list_events(
        entity_type="midseason.delisting", entity_id=delisting.delisting_id, action="midseason.delisting.submitted"
    )[0]
    assert event.actor_type == "anonymous_operator"
    assert event.actor_role == "replay_operator"


# -- vacancy_allocations: pure unit coverage --------------------------------


def test_vacancy_allocations_skips_teams_once_satisfied_and_stays_in_confirmed_order():
    allocations = list(vacancy_allocations(["a", "b", "c"], {"a": 2, "b": 0, "c": 1}))
    assert [(overall, round_number, position, entry) for overall, round_number, position, entry, _ in allocations] == [
        (1, 1, 1, "a"),
        (2, 1, 2, "c"),
        (3, 2, 1, "a"),
    ]


def test_vacancy_allocations_with_no_vacancies_yields_nothing():
    assert list(vacancy_allocations(["a", "b"], {"a": 0, "b": 0})) == []


# -- Regression: app.draft generalisation (draft_kind) ----------------------


def test_preseason_and_midseason_drafts_coexist_independently_for_one_season():
    """`season_draft`'s generalised `draft_kind` (issue #164) must let one
    season carry a mid-season draft alongside its original preseason draft,
    each independently addressable, without the newer row colliding with or
    shadowing the older one."""
    ctx = _delisting_open(trigger_round=10, squad_limit=3)
    database, season, entries = ctx["database"], ctx["season"], ctx["entries"]
    drafts = DraftRepository(database)

    worst = entries[9]
    squad = ctx["midseason"].ownership.current_squad(worst.season_entry_id)
    ctx["midseason"].submit_delisting(season.season_id, worst.season_entry_id, squad[0].season_player_id, actor=ACTOR)
    ctx["midseason"].lock_delistings(season.season_id, actor=ACTOR)
    ctx["midseason"].generate_selection_table(season.season_id, actor=ACTOR)

    assert drafts.status(season.season_id, draft_kind="preseason") is None
    midseason_status = drafts.status(season.season_id, draft_kind="midseason")
    assert midseason_status is not None and midseason_status.draft_kind == "midseason"

    # A season may still separately accept an (unrelated) preseason draft
    # order -- it is keyed by (season_id, draft_kind), not season_id alone,
    # and does not observe or block on the mid-season draft's own state.
    preseason_draft_id = drafts.accept_order(season.season_id, [e.season_entry_id for e in entries], actor=ACTOR)
    preseason_status = drafts.status(season.season_id, draft_kind="preseason")
    assert preseason_status is not None
    assert preseason_status.draft_id == preseason_draft_id
    assert preseason_status.draft_id != midseason_status.draft_id

    # A second preseason draft for the same season is still refused (the
    # per-kind uniqueness constraint, not a per-season one).
    with pytest.raises(Exception):
        drafts.accept_order(season.season_id, [e.season_entry_id for e in entries], actor=ACTOR)
    # ...and a second mid-season draft is independently and separately
    # refused by app.midseason_draft's own check.
    with pytest.raises(MidseasonDraftStateError):
        ctx["midseason"].confirm_ladder(season.season_id, ctx["competition"].competition_id, actor=ACTOR)


def test_midseason_execute_pick_bypasses_the_closed_preseason_window():
    """A realistic 2026 replay season already has its preseason window
    closed (draft.py's `execute_pick` must pass `allow_closed_window=True`
    for a non-preseason draft_kind, or every mid-season selection would
    fail with `PreseasonWindowClosedError`)."""
    from app.preseason import PreseasonRepository

    ctx = _delisting_open(trigger_round=10, squad_limit=3)
    database, season, entries = ctx["database"], ctx["season"], ctx["entries"]
    m = ctx["midseason"]

    # Simulate an already-closed preseason window for this season, as a
    # real post-round-10 season would have.
    drafts = DraftRepository(database)
    preseason_entries = [e.season_entry_id for e in entries]
    for entry in entries:
        for period in list(m.ownership.current_squad(entry.season_entry_id)):
            m.ownership.release(period.season_player_id, actor=ACTOR)
    drafts.accept_order(season.season_id, preseason_entries, actor=ACTOR)
    for _ in range(len(preseason_entries) * 3):
        pick = drafts.next_pick(season.season_id)
        player = m.player_pool.refresh_player(
            season.season_id, 8_000_000 + pick.overall_number, f"Preseason player {pick.overall_number}"
        )
        drafts.execute_pick(season.season_id, pick.current_season_entry_id, player.season_player_id, actor=ACTOR)
    drafts.finalize(season.season_id, actor=ACTOR)
    preseason = PreseasonRepository(database)
    preseason.open_window(season.season_id, actor=ACTOR)
    preseason.close_window(season.season_id, actor=ACTOR)

    worst = entries[9]
    squad = m.ownership.current_squad(worst.season_entry_id)
    m.submit_delisting(season.season_id, worst.season_entry_id, squad[0].season_player_id, actor=ACTOR)
    m.lock_delistings(season.season_id, actor=ACTOR)
    m.generate_selection_table(season.season_id, actor=ACTOR)

    fresh = m.player_pool.refresh_player(season.season_id, 9_000_001, "Mid-season draftee")
    nxt = m.next_pick(season.season_id)
    # This must succeed despite the preseason window being closed.
    completed = m.execute_pick(season.season_id, nxt.current_season_entry_id, fresh.season_player_id, actor=ACTOR)
    assert completed.completed_at is not None


# -- Season config: mid-season draft trigger round ---------------------------


def test_set_midseason_draft_trigger_round_is_audited_and_frozen_once_a_draft_exists():
    ctx = build_season(trigger_round=10)
    seasons = SeasonRepository(ctx["database"])
    season = seasons.set_midseason_draft_trigger_round(ctx["season"].season_id, 10, actor=ACTOR, reason="season setup")
    assert season.midseason_draft_trigger_round == 10

    events = AuditEventRepository(ctx["database"]).list_events(action="season.midseason_draft_trigger_round.set")
    assert len(events) == 1

    m = MidseasonDraftRepository(ctx["database"])
    m.confirm_ladder(ctx["season"].season_id, ctx["competition"].competition_id, actor=ACTOR)
    with pytest.raises(ValueError):
        seasons.set_midseason_draft_trigger_round(ctx["season"].season_id, 9, actor=ACTOR, reason="change my mind")
