"""Issue #144: ordinary weekly-lineup submission is permitted while the
BBBFFL round lifecycle is `open` *or* `live` -- `live` (at least one AFL
match has started) is never itself a global submission lock. Every
submission attempted while `live` still passes through the same
authoritative position-level lock guard (`app.lockouts`) that governs
`open`, with identical selective/main trigger enforcement, Opening Round
deferred-position protection, atomicity and concurrency guarantees.

See `app.lineups`'s module docstring ("Round lifecycle vs. position-level
lock state") and docs/lockouts.md's "Round lifecycle and position-level
lock state are independent" section for the design this exercises.
"""

from datetime import timedelta

import pytest

from app.audit import ActorContext
from app.carry_forward import CarryForwardService
from app.lineup_proxy import LineupProxyService
from app.lineups import LineupConflictError, LineupIntegrityError, RoundPublishedError, WeeklyLineupRepository
from app.lockouts import LockedSelectionError, LockoutRepository, LockoutTriggerRepository
from app.opening_round import DeferredSlotLockedError, OpeningRoundNominationRepository, OpeningRoundSelectionGuard
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests.test_carry_forward import context as carry_forward_context
from tests.test_competition_lifecycle import scores
from tests.test_lineups import context as plain_context
from tests.test_lineups import save as plain_save
from tests.test_lockouts import (
    ALL_MATCHES,
    EARLY_HOME,
    EARLY_MATCH_ID,
    EARLY_START,
    LATE_HOME,
    LATE_MATCH_ID,
    LATE_START,
    UNCOVERED_HOME,
    FakeMatchFacts,
    acquire,
    configure_main,
    configure_selective,
    context,
    edit_draft,
    establish,
)
from tests.test_opening_round import nominate_bl_2024, setup_scope

SCORER = ActorContext.anonymous_operator("scorer")


# ---------------------------------------------------------------------------
# 1. `open` before any selective trigger -- legal ordinary submission succeeds.
# ---------------------------------------------------------------------------


def test_open_before_any_trigger_ordinary_submission_succeeds():
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    configure_selective(
        LockoutTriggerRepository(db), round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1
    )
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    guard = LockoutRepository(db).guard(
        match_facts=FakeMatchFacts(ALL_MATCHES), evaluation_at=EARLY_START - timedelta(days=1)
    )
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=guard)
    assert submitted.version == 1
    assert submitted.positions["F1"] == early.season_player_id
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "open"


# ---------------------------------------------------------------------------
# 2. `live` with only a selective trigger activated -- changing only
#    unlocked positions succeeds.
# ---------------------------------------------------------------------------


def test_live_with_only_selective_trigger_activated_unlocked_positions_remain_editable():
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    late = acquire(pool, ownership, scope, entry, 2, LATE_HOME)
    other_late = acquire(pool, ownership, scope, entry, 3, LATE_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=1))
    draft, submitted = establish(
        lineups, round_, entry, scope, {"F1": early.season_player_id, "M1": late.season_player_id}, guard=pre_guard
    )

    lifecycle.transition(round_.bbbffl_round_id, "live")

    live_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    edit = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": early.season_player_id, "M1": other_late.season_player_id},
        from_revision=draft.revision,
    )
    changed = lineups.submit(
        edit.lineup_id,
        expected_draft_revision=edit.revision,
        expected_submission_version=submitted.version,
        lock_guard=live_guard,
    )
    assert changed.version == 2
    assert changed.positions["F1"] == early.season_player_id
    assert changed.positions["M1"] == other_late.season_player_id
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "live"


# ---------------------------------------------------------------------------
# 3. An intentionally vacant unlocked position can be filled while `live`.
# ---------------------------------------------------------------------------


def test_vacant_unlocked_position_can_be_filled_while_live():
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    configure_selective(
        LockoutTriggerRepository(db), round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1
    )
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    uncovered = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=1))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_guard)
    assert submitted.positions["M1"] is None  # deliberate vacancy

    lifecycle.transition(round_.bbbffl_round_id, "live")

    live_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    edit = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": early.season_player_id, "M1": uncovered.season_player_id},
        from_revision=draft.revision,
    )
    filled = lineups.submit(
        edit.lineup_id,
        expected_draft_revision=edit.revision,
        expected_submission_version=submitted.version,
        lock_guard=live_guard,
    )
    assert filled.positions["M1"] == uncovered.season_player_id
    assert filled.positions["F1"] == early.season_player_id


# ---------------------------------------------------------------------------
# 4. A selectively locked player/position cannot be added, removed, moved,
#    or swapped while `live`.
# ---------------------------------------------------------------------------


def test_selectively_locked_position_cannot_be_added_removed_moved_or_swapped_while_live():
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    configure_selective(
        LockoutTriggerRepository(db), round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1
    )
    incumbent = acquire(pool, ownership, scope, entry, 1, EARLY_HOME, name="Incumbent")
    challenger = acquire(pool, ownership, scope, entry, 2, EARLY_HOME, name="Challenger")
    uncovered = acquire(pool, ownership, scope, entry, 3, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=1))
    draft, submitted = establish(
        lineups,
        round_,
        entry,
        scope,
        {"F1": incumbent.season_player_id, "M1": uncovered.season_player_id},
        guard=pre_guard,
    )

    lifecycle.transition(round_.bbbffl_round_id, "live")
    live_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))

    current_revision = draft.revision

    def attempt(positions):
        nonlocal current_revision
        edit = edit_draft(lineups, round_, entry, scope, draft.lineup_id, positions, from_revision=current_revision)
        current_revision = edit.revision
        lineups.submit(
            edit.lineup_id,
            expected_draft_revision=edit.revision,
            expected_submission_version=submitted.version,
            lock_guard=live_guard,
        )

    # move/swap: locked incumbent moved out of F1 into M1.
    with pytest.raises(LockedSelectionError):
        attempt({"F1": uncovered.season_player_id, "M1": incumbent.season_player_id})
    # remove: F1 cleared to vacant.
    with pytest.raises(LockedSelectionError):
        attempt({"F1": None, "M1": uncovered.season_player_id})
    # swap within the same locked match: incumbent replaced by challenger.
    with pytest.raises(LockedSelectionError):
        attempt({"F1": challenger.season_player_id, "M1": uncovered.season_player_id})
    # add: a new player from the locked match introduced into a fresh slot.
    with pytest.raises(LockedSelectionError):
        attempt(
            {
                "F1": incumbent.season_player_id,
                "M1": uncovered.season_player_id,
                "M2": challenger.season_player_id,
            }
        )

    assert lineups.get_effective_submission(draft.lineup_id).version == submitted.version
    assert lineups.get_effective_submission(draft.lineup_id).positions == submitted.positions


# ---------------------------------------------------------------------------
# 5. A mixed atomic submission containing one legal unlocked change plus one
#    locked change fails entirely with no partial write.
# ---------------------------------------------------------------------------


def test_mixed_atomic_submission_with_one_legal_and_one_locked_change_fails_entirely():
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    configure_selective(
        LockoutTriggerRepository(db), round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1
    )
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    uncovered = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME)
    other_uncovered = acquire(pool, ownership, scope, entry, 3, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=1))
    draft, submitted = establish(
        lineups,
        round_,
        entry,
        scope,
        {"F1": early.season_player_id, "M1": uncovered.season_player_id},
        guard=pre_guard,
    )

    lifecycle.transition(round_.bbbffl_round_id, "live")
    live_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))

    mixed = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": None, "M1": other_uncovered.season_player_id},  # M1: legal; F1: illegal (clears a locked position)
        from_revision=draft.revision,
    )
    with pytest.raises(LockedSelectionError):
        lineups.submit(
            mixed.lineup_id,
            expected_draft_revision=mixed.revision,
            expected_submission_version=submitted.version,
            lock_guard=live_guard,
        )

    effective = lineups.get_effective_submission(draft.lineup_id)
    assert effective.version == submitted.version == 1
    assert effective.positions == submitted.positions
    assert lineups.get_submission(draft.lineup_id, 2) is None


# ---------------------------------------------------------------------------
# 6. After the main/remaining trigger activates, all remaining ordinary
#    changes are rejected even though lifecycle may still be `live`.
# ---------------------------------------------------------------------------


def test_main_trigger_activation_locks_all_remaining_changes_while_lifecycle_stays_live():
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    triggers = LockoutTriggerRepository(db)
    configure_selective(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    configure_main(triggers, round_.bbbffl_round_id, [LATE_MATCH_ID], sequence=2)
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    uncovered = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME)
    other_uncovered = acquire(pool, ownership, scope, entry, 3, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=1))
    draft, submitted = establish(
        lineups,
        round_,
        entry,
        scope,
        {"F1": early.season_player_id, "M1": uncovered.season_player_id},
        guard=pre_guard,
    )

    lifecycle.transition(round_.bbbffl_round_id, "live")

    mid_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    edit = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": early.season_player_id, "M1": other_uncovered.season_player_id},
        from_revision=draft.revision,
    )
    changed = lineups.submit(
        edit.lineup_id,
        expected_draft_revision=edit.revision,
        expected_submission_version=submitted.version,
        lock_guard=mid_guard,
    )
    assert changed.positions["M1"] == other_uncovered.season_player_id

    post_main_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=LATE_START)
    final_edit = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": early.season_player_id, "M1": uncovered.season_player_id},
        from_revision=edit.revision,
    )
    with pytest.raises(LockedSelectionError, match="main_lockout_triggered"):
        lineups.submit(
            final_edit.lineup_id,
            expected_draft_revision=final_edit.revision,
            expected_submission_version=changed.version,
            lock_guard=post_main_guard,
        )

    assert lifecycle.get_round(round_.bbbffl_round_id).state == "live"
    assert lineups.get_effective_submission(draft.lineup_id).version == changed.version


# ---------------------------------------------------------------------------
# 7. Opening Round deferred/preloaded positions remain immutable during
#    `live`.
# ---------------------------------------------------------------------------


def test_opening_round_deferred_positions_remain_immutable_during_live():
    from tests.db_helpers import migrated_connection

    db = migrated_connection()
    lifecycle, round_, entries, scope = setup_scope(db, 2401, 2401)
    entry = entries[0]
    rule, deferred_player, nomination = nominate_bl_2024(
        db, scope["season_id"], round_.bbbffl_round_id, entry, position="M1", canonical_id=970001
    )
    pool, ownership = PlayerPoolRepository(db), OwnershipRepository(db)
    configure_selective(
        LockoutTriggerRepository(db), round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1
    )
    early = acquire(pool, ownership, scope, entry, 970002, EARLY_HOME)
    displacer = acquire(pool, ownership, scope, entry, 970003, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    nominations = OpeningRoundNominationRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_lock = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=1))
    draft, submitted = establish(
        lineups,
        round_,
        entry,
        scope,
        {"F1": early.season_player_id, "M1": deferred_player.season_player_id},
        guard=OpeningRoundSelectionGuard(nominations, pre_lock),
    )
    assert submitted.positions["M1"] == deferred_player.season_player_id

    lifecycle.transition(round_.bbbffl_round_id, "live")

    live_lock = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    live_guard = OpeningRoundSelectionGuard(nominations, live_lock)
    edit = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": early.season_player_id, "M1": displacer.season_player_id},
        from_revision=draft.revision,
    )
    with pytest.raises(DeferredSlotLockedError):
        lineups.submit(
            edit.lineup_id,
            expected_draft_revision=edit.revision,
            expected_submission_version=submitted.version,
            lock_guard=live_guard,
        )
    assert lineups.get_effective_submission(draft.lineup_id).positions["M1"] == deferred_player.season_player_id


# ---------------------------------------------------------------------------
# 8. Coach and delegated/proxy submission paths -- and draft/carry-forward --
#    exhibit the same live-round lock behaviour.
# ---------------------------------------------------------------------------


def test_coach_proxy_and_carry_forward_paths_share_live_round_lock_enforcement():
    db, lifecycle, round_ids, entries, scope, pool, ownership = carry_forward_context(
        year=2410, rounds=2, squad_limit=20
    )
    source_round_id, target_round_id = round_ids
    configure_selective(LockoutTriggerRepository(db), target_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=1))
    live_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    lineups = WeeklyLineupRepository(db)

    # -- Carry-forward path: a source-round submission naming a now-locked
    # player cannot be carried into the live target round; one naming only
    # an unlocked player can.
    locked_source_player = acquire(pool, ownership, scope, entries[0], 9101, EARLY_HOME)
    unlocked_source_player = acquire(pool, ownership, scope, entries[1], 9102, UNCOVERED_HOME)
    for entry, player in ((entries[0], locked_source_player), (entries[1], unlocked_source_player)):
        source_draft = lineups.save_draft(
            scope["season_id"],
            scope["competition_id"],
            source_round_id,
            entry.season_entry_id,
            {"F1": player.season_player_id},
            expected_revision=0,
        )
        lineups.submit(
            source_draft.lineup_id, expected_draft_revision=source_draft.revision, expected_submission_version=0
        )

    # -- Coach path baseline, established on the target round while `open`.
    coach_early = acquire(pool, ownership, scope, entries[2], 9201, EARLY_HOME)
    coach_draft = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        target_round_id,
        entries[2].season_entry_id,
        {"F1": coach_early.season_player_id},
        expected_revision=0,
    )
    coach_submitted = lineups.submit(
        coach_draft.lineup_id,
        expected_draft_revision=coach_draft.revision,
        expected_submission_version=0,
        lock_guard=pre_guard,
    )

    # -- Proxy path baseline, established via LineupProxyService while `open`.
    proxy_early = acquire(pool, ownership, scope, entries[3], 9301, EARLY_HOME)
    proxy = LineupProxyService(db)
    proxy_draft = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        target_round_id,
        entries[3].season_entry_id,
        {"F1": proxy_early.season_player_id},
        expected_revision=0,
        actor=SCORER,
    )
    proxy_submitted = proxy.submit(
        proxy_draft.lineup_id,
        expected_draft_revision=proxy_draft.revision,
        expected_submission_version=0,
        actor=SCORER,
        reason="delegated entry",
        lock_guard=pre_guard,
    )

    lifecycle.transition(target_round_id, "live")

    # Carry-forward: locked source rejected, unlocked source accepted.
    carry = CarryForwardService(db)
    with pytest.raises(LockedSelectionError):
        carry.carry_forward(
            scope["season_id"],
            scope["competition_id"],
            target_round_id,
            entries[0].season_entry_id,
            expected_submission_version=0,
            actor=ActorContext.system(),
            reason="carry",
            lock_guard=live_guard,
        )
    carried, _ = carry.carry_forward(
        scope["season_id"],
        scope["competition_id"],
        target_round_id,
        entries[1].season_entry_id,
        expected_submission_version=0,
        actor=ActorContext.system(),
        reason="carry",
        lock_guard=live_guard,
    )
    assert carried.positions["F1"] == unlocked_source_player.season_player_id

    # Coach: an edit touching the now-locked F1 is refused identically.
    coach_edit = lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        target_round_id,
        entries[2].season_entry_id,
        {"F1": None},
        expected_revision=coach_draft.revision,
    )
    with pytest.raises(LockedSelectionError):
        lineups.submit(
            coach_edit.lineup_id,
            expected_draft_revision=coach_edit.revision,
            expected_submission_version=coach_submitted.version,
            lock_guard=live_guard,
        )

    # Proxy: the same change, attempted through LineupProxyService, is
    # refused identically -- no scorer/admin/replay-operator exemption.
    proxy_edit = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        target_round_id,
        entries[3].season_entry_id,
        {"F1": None},
        expected_revision=proxy_draft.revision,
        actor=SCORER,
    )
    with pytest.raises(LockedSelectionError):
        proxy.submit(
            proxy_edit.lineup_id,
            expected_draft_revision=proxy_edit.revision,
            expected_submission_version=proxy_submitted.version,
            actor=SCORER,
            reason="attempted change",
            lock_guard=live_guard,
        )


# ---------------------------------------------------------------------------
# 9 & 10. `review` and `final` continue rejecting ordinary submissions.
# ---------------------------------------------------------------------------


def test_review_state_rejects_ordinary_submission():
    db, lifecycle, round_, entries, scope, players, _ = plain_context()
    repo = WeeklyLineupRepository(db)
    draft = plain_save(repo, round_, entries, scope, {"F1": players[0].season_player_id})
    submitted = repo.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)
    lifecycle.transition(round_.bbbffl_round_id, "live")
    lifecycle.transition(round_.bbbffl_round_id, "review")
    edit = plain_save(repo, round_, entries, scope, {"M1": players[1].season_player_id}, draft.revision)
    with pytest.raises(LineupIntegrityError, match="does not currently permit"):
        repo.submit(
            edit.lineup_id, expected_draft_revision=edit.revision, expected_submission_version=submitted.version
        )


def test_final_state_rejects_ordinary_submission():
    db, lifecycle, round_, entries, scope, players, _ = plain_context()
    repo = WeeklyLineupRepository(db)
    draft = plain_save(repo, round_, entries, scope, {"F1": players[0].season_player_id})
    submitted = repo.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)
    lifecycle.transition(round_.bbbffl_round_id, "live")
    lifecycle.transition(round_.bbbffl_round_id, "review")
    lifecycle.publish_results(round_.bbbffl_round_id, scores(lifecycle, round_.bbbffl_round_id), reason="approved")
    edit = plain_save(repo, round_, entries, scope, {"M1": players[1].season_player_id}, draft.revision)
    with pytest.raises(RoundPublishedError):
        repo.submit(
            edit.lineup_id, expected_draft_revision=edit.revision, expected_submission_version=submitted.version
        )


# ---------------------------------------------------------------------------
# 11. The audited locked-lineup correction workflow remains separate and
#     unchanged.
# ---------------------------------------------------------------------------


def test_locked_lineup_correction_workflow_remains_separate_and_unchanged_during_live():
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    configure_selective(
        LockoutTriggerRepository(db), round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1
    )
    early = acquire(pool, ownership, scope, entry, 1, EARLY_HOME)
    challenger = acquire(pool, ownership, scope, entry, 2, EARLY_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=1))
    draft, submitted = establish(lineups, round_, entry, scope, {"F1": early.season_player_id}, guard=pre_guard)

    lifecycle.transition(round_.bbbffl_round_id, "live")
    live_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))

    # Ordinary submission still cannot touch the now-locked F1 -- issue #144
    # does not weaken that in any way.
    edit = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"F1": challenger.season_player_id},
        from_revision=draft.revision,
    )
    with pytest.raises(LockedSelectionError):
        lineups.submit(
            edit.lineup_id,
            expected_draft_revision=edit.revision,
            expected_submission_version=submitted.version,
            lock_guard=live_guard,
        )

    # The authorised correction workflow is the one, unchanged door that can
    # override an already-locked position -- a lock_guard supplied here is
    # only ever used for materialize()/provenance, never to reject.
    corrected = lineups.submit_correction(
        draft.lineup_id,
        {"F1": challenger.season_player_id},
        expected_submission_version=submitted.version,
        actor=SCORER,
        reason="league-approved correction",
        lock_guard=live_guard,
    )
    assert corrected.to_version == submitted.version + 1
    assert lineups.get_effective_submission(draft.lineup_id).positions["F1"] == challenger.season_player_id
    assert corrected.slots[0].was_locked is True


# ---------------------------------------------------------------------------
# 12. Submission versions, actor/source provenance, and stale-version
#     protection remain intact.
# ---------------------------------------------------------------------------


def test_submission_provenance_and_stale_version_protection_remain_intact_during_live():
    db, lifecycle, round_, entries, scope, pool, ownership = context()
    entry = entries[0]
    configure_selective(
        LockoutTriggerRepository(db), round_.bbbffl_round_id, [EARLY_MATCH_ID], key="early-1", sequence=1
    )
    uncovered = acquire(pool, ownership, scope, entry, 1, UNCOVERED_HOME)
    other_uncovered = acquire(pool, ownership, scope, entry, 2, UNCOVERED_HOME)
    lineups = WeeklyLineupRepository(db)
    matches = FakeMatchFacts(ALL_MATCHES)
    pre_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START - timedelta(days=1))
    draft, submitted = establish(lineups, round_, entry, scope, {"M1": uncovered.season_player_id}, guard=pre_guard)
    assert submitted.version == 1
    assert submitted.source_type == "coach"

    lifecycle.transition(round_.bbbffl_round_id, "live")
    live_guard = LockoutRepository(db).guard(match_facts=matches, evaluation_at=EARLY_START + timedelta(minutes=1))
    edit = edit_draft(
        lineups,
        round_,
        entry,
        scope,
        draft.lineup_id,
        {"M1": other_uncovered.season_player_id},
        from_revision=draft.revision,
    )

    with pytest.raises(LineupConflictError, match="stale submission version"):
        lineups.submit(
            edit.lineup_id, expected_draft_revision=edit.revision, expected_submission_version=0, lock_guard=live_guard
        )

    changed = lineups.submit(
        edit.lineup_id,
        expected_draft_revision=edit.revision,
        expected_submission_version=submitted.version,
        actor=ActorContext.coach("coach-42"),
        reason="weekly change",
        lock_guard=live_guard,
    )
    assert changed.version == 2
    assert changed.actor_type == "coach" and changed.actor_id == "coach-42"
    assert changed.source_type == "coach"
    assert changed.reason == "weekly change"
    assert lineups.get_submission(draft.lineup_id, 1) == submitted
