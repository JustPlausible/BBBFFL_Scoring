"""Issue #211 workflow improvements A and B: deriving SS1-SS4's lockout plan
from the concurrent finals week, and the single paired web "Open week"
operator action built on top of it. This module sits directly above both
`app.finals`/`app.finals_preflight` (FINALS/FINALS_PREFLIGHT) and
`app.superscore_round` (SUPERSCORE) *and* `app.lockouts` (LOCKOUTS) --
`app.superscore_round`/`app.superscore_review` must themselves stay siblings
of lockouts (see `tests/test_architecture.py`'s
`test_superscore_does_not_depend_on_routes_grand_final_lockouts_or_
composition_root`), so the trigger-derivation logic that necessarily
touches both SuperScore's own round/mapping model and
`app.lockouts.LockoutTriggerRepository` lives here, one layer above both,
rather than inside `app.superscore_round` itself -- the same shape
`app.finals_superscore_dashboard` (issue #208) already uses to compose
finals and SuperScore without either depending on the other.

Workflow A -- `synchronise_lockout_plan_from_finals` -- derives/synchronises
one SS round's AFL mapping and lockout-trigger plan from the *exact*
concurrent finals week's own current plan, rather than requiring a Scorer
to manually duplicate the identical configuration onto SuperScore by hand a
second time (confirmed replay defect: "SS1 initially had no lockout
plan... Manually configuring the identical plan on SS1 immediately
resolved" it). Reuses only existing mature infrastructure, never a parallel
implementation of any of it:

- `app.superscore_round.resolve_concurrent_finals_afl_mapping` (unchanged)
  to find the exact concurrent finals week and its frozen AFL mapping --
  the same season/week-number derivation `confirm_afl_mapping`'s own
  docstring already recommends sourcing from;
- `app.superscore_round.confirm_afl_mapping` (unchanged) to accept/correct
  SS's own AFL mapping onto that same evidence -- SS retains its own
  persisted `round_afl_mapping` row throughout, never the finals week's;
- `app.lockouts.LockoutTriggerRepository.list_triggers`/`.configure`
  (unchanged) to read the finals week's current trigger definitions and
  persist SS's own trigger-revision rows from them -- SS keeps a fully
  separate set of `bbbffl_round_lockout_trigger`/`..._revision` rows, keyed
  by its own `ss_round_id`, never a shared or aliased row.

Idempotent: a trigger already matching the finals week's current
`(trigger_type, sequence, afl_match_ids)` for the same `trigger_key` is
left untouched -- `configure()` always advances a trigger's revision
unconditionally, so calling it for an unchanged trigger would create a
spurious new SS revision row on every re-run; this compares first and only
calls `configure()` for a key that is new or has actually diverged.
Re-running this with nothing changed on the finals side is therefore a true
no-op (no new SS trigger revisions), and re-running it after the finals
plan changes updates only the diverged trigger keys, never a wholesale
replace. Audited: whichever mapping/trigger writes actually occur emit
their own existing audit events (`app.round_mapping`/`app.lockouts`'s own),
and this function always additionally records one
`superscore.lockout_plan.synchronised_from_finals` event summarising the
outcome, so the *derivation relationship itself* -- not just the individual
writes it may have produced -- is auditable.

Workflow B -- `open_finals_and_superscore_week` -- is one paired web "Open
week" operator action for a concurrent finals week and its SS round,
layered *over* -- never replacing -- the two genuinely independent
lifecycle domains. It reuses `app.finals_preflight.build_finals_week_
preflight`/`open_finals_week` (unchanged) for finals readiness/opening,
`synchronise_lockout_plan_from_finals` above for SS's lockout plan, and
`app.superscore_round.setup_round`/`open_round` (unchanged, the exact calls
`scripts/superscore_round_2026.py` already makes from the CLI) for SS's
lifecycle open.

Ordering deliberately opens the finals week *before* synchronising SS's
lockout plan, even though issue #211's own suggested flow lists
"synchronise, then open Finals" -- `synchronise_lockout_plan_from_finals`
calls `resolve_concurrent_finals_afl_mapping`, which by design only reads
the finals week's AFL mapping once it is *frozen* onto that round's own
`bbbffl_round_lifecycle` row (only `open_finals_week` creates that row),
specifically to avoid ever deriving SS's mapping from a pre-open mapping
that could still be corrected before the week actually opens. Opening
finals first is what lets this reuse that existing, already-correct
function unchanged rather than adding a second, weaker mapping read.

`open_finals_and_superscore_week` validates the pairing before mutating
anything: a finals bracket must resolve for `bracket_id`, a SuperScore
round must already be configured for the same season/week number, and
(when not already open) the finals week's own preflight must be safe to
open. Nothing is written if the pairing itself is invalid.

Idempotent against a lifecycle half that is already open: this action
re-runs the SS lockout-plan synchronisation every call (itself idempotent),
but only actually opens whichever half(s) are still
`not_created`/`upcoming` -- calling this again on an already-fully-open
pairing just re-confirms/refreshes the SS lockout plan and reports both
halves' current state, rather than raising.

Each half still opens through its own existing, independent service call,
in its own transaction, recording its own separate audit event under its
own entity id -- there is no shared lifecycle row and no fabricated joint
transition. "Paired" describes only this operator workflow's single web
action; the underlying domain model is exactly as separate as it was
before this module existed.

## Current operational constraint (issue #214)

`_synchronise_triggers_locked` serialises the trigger-plan read/validate/
write against both rounds' own row locks (issue #211, Codex review,
rounds 6-8), but that boundary does not yet also cover `confirm_afl_mapping`
(SS's mutable mapping head) or SS's *frozen* `bbbffl_round_lifecycle`
mapping (only ever written once, by `app.superscore_round.setup_round` ->
`create_non_ordinary_round`). A `setup_round()` freezing SS's mapping
concurrently with this module's own unlocked `frozen_round` pre-check in
`synchronise_lockout_plan_from_finals`, or a mapping correction committing
just before the final locked trigger recheck detects a concurrent trigger
divergence, are both real, narrow, low-likelihood races this module does
not yet close -- see issue #214 for the full analysis and the desired
broader transaction-boundary redesign (mapping + frozen lifecycle state +
trigger plan, all one serialised decision). Deliberately not chased
further here: closing it needs a higher-level orchestration boundary
across `app.round_mapping`, `app.competition_lifecycle` and
`app.lockouts` together, not another piecemeal per-repository lock.

**Until #214 lands, treat paired Finals/SuperScore opening as a
single-operator administrative action**: do not run it concurrently with
a separate `setup_round()`, mapping correction, or lockout-trigger
configuration call for either member of the same paired week. This
matches the current 2026 replay's actual operating model (one operator,
sequential administrative actions), under which this module's fixed races
(issue #211, Codex review, rounds 2-8: frozen-mapping divergence,
main-trigger/coverage validation, activation preflight, stale-evidence
rejection, atomic multi-trigger writes, and the two round-lock TOCTOU
closures) are the ones that actually matter.
"""

from contextlib import nullcontext

from app.afl_client import AflApiError
from app.audit import ActorContext, append_event
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import _for_update_suffix, transaction
from app.finals import FinalsBracketRepository
from app.finals_preflight import build_finals_week_preflight, open_finals_week
from app.lockouts import LockoutTriggerRepository, TriggerAlreadyActivatedError, TriggerAlreadyRemovedError
from app.round_mapping import AflApiReferenceValidator, AflReferenceValidator, RoundMappingRepository
from app.superscore_round import confirm_afl_mapping, open_round, resolve_concurrent_finals_afl_mapping, setup_round

__all__ = [
    "FrozenMappingDivergedError",
    "LockoutPlanDivergedError",
    "PairedOpenWeekError",
    "open_finals_and_superscore_week",
    "synchronise_lockout_plan_from_finals",
]


class PairedOpenWeekError(Exception):
    """The finals/SuperScore pairing failed validation -- nothing was
    opened and nothing was mutated."""


class FrozenMappingDivergedError(Exception):
    """SS's round already has a frozen `bbbffl_round_lifecycle.afl_*`
    mapping snapshot -- recorded once, when `app.superscore_round.
    setup_round` first calls `create_non_ordinary_round` -- that diverges
    from the concurrent finals week's current accepted mapping. Correcting
    only the mutable `round_afl_mapping` head (`confirm_afl_mapping`) never
    reaches that frozen snapshot: every reader of an already-set-up round
    (`app.calculations._round_context`'s `l.afl_round_id`, `l.*`, exactly
    as `resolve_concurrent_finals_afl_mapping`'s own docstring explains for
    the finals side) consults the frozen row, never a fresh `resolve()`. So
    updating just the head here would misreport a successful
    synchronisation while the round kept calculating against its old,
    frozen AFL round. Nothing is mutated; an operator must resolve the
    divergence directly (e.g. correct the finals week's mapping back to
    what SS already froze, or rebuild SS's round) before synchronisation
    can proceed."""


class LockoutPlanDivergedError(Exception):
    """SS's persisted lockout-trigger plan cannot be safely/automatically
    reconciled with the finals week's current one -- nothing was mutated;
    an operator must resolve the divergence directly (e.g. via
    `app.lockouts.LockoutTriggerRepository`/the round-preflight lockout
    form) before synchronisation can proceed. Raised for three distinct
    reasons (see `synchronise_lockout_plan_from_finals`): an SS trigger key
    with no corresponding key at all on the finals side, whether still
    configured or removed there (never created on finals, e.g. an SS-only
    trigger an operator configured directly -- genuinely nothing to
    reconcile automatically); an obsolete SS trigger key the finals plan
    removed (issue #219) that has itself already activated on SS, so it can
    never be removed to match; or a genuine sequence-reordering cycle (e.g.
    two trigger keys swapping sequences) that cannot be applied one trigger
    at a time without a transient collision.

    Issue #219: an SS trigger key the finals plan simply no longer has
    *because it was explicitly removed there* is no longer one of these
    unresolvable cases -- `LockoutTriggerRepository.remove` gives this
    synchronisation a safe way to mirror that removal onto SS automatically
    (see `_validate_trigger_sync_plan`'s `keys_to_remove`), rather than
    failing closed the way an SS-only key with no finals counterpart at all
    still correctly does."""


def _plan_trigger_sync(ss_round_id, finals_triggers, ss_triggers_by_key):
    """Pure, no-I/O planning pass for `_validate_trigger_sync_plan` --
    determines a safe application order for every given trigger, without
    touching the database. Issue #211 P1 (Codex review, round 2): the
    previous version interleaved this planning with the actual
    `configure()` writes, one multi-pass loop at a time -- each
    `configure()` call commits its own transaction, so a later pass
    discovering a genuine, unresolvable cycle (e.g. a two-key sequence
    swap) still left every trigger *already* applied in an earlier pass
    committed, violating this function's own fail-closed, nothing-mutated
    contract for a plan a real operator could construct (a free move
    alongside a genuine swap). Planning the whole move graph first, purely
    in memory, means a cycle is detected and raised *before* any write
    happens at all -- the only write phase left (`_apply_trigger_sync`
    below) replays an already-fully-validated, guaranteed-resolvable
    order.

    Issue #211 P1 (Codex review, round 3): modelling only sequence
    occupancy is not enough -- `LockoutTriggerRepository.configure()`
    also enforces that a selective trigger's sequence precede every main
    trigger's, and a main trigger's sequence follow every selective
    trigger's, checked against whatever is *currently persisted* at write
    time. A plan this function judged occupancy-safe could still have a
    real `configure()` call rejected by that ordering rule partway
    through, exactly as fail-unsafe as the cycle case above. Tracking each
    simulated trigger's `(trigger_type, sequence)` and re-checking both
    ordering rules before treating a move as applicable -- not just
    whether its target sequence is free -- catches that here too, and can
    itself require reordering the plan (e.g. moving `main` out of the way
    before a selective trigger can move past its old position) exactly as
    the occupancy check already does."""
    simulated = {key: (trigger.trigger_type, trigger.sequence) for key, trigger in ss_triggers_by_key.items()}
    ordered: list = []
    remaining = list(finals_triggers)
    progressed = True
    while remaining and progressed:
        progressed = False
        still_remaining = []
        for trigger in remaining:
            others = {key: state for key, state in simulated.items() if key != trigger.trigger_key}
            occupied = {seq for _type, seq in others.values()}
            if trigger.sequence in occupied:
                still_remaining.append(trigger)
                continue
            if trigger.trigger_type == "selective":
                main_sequences = [seq for ttype, seq in others.values() if ttype == "main"]
                if main_sequences and trigger.sequence >= min(main_sequences):
                    still_remaining.append(trigger)
                    continue
            elif trigger.trigger_type == "main":
                selective_sequences = [seq for ttype, seq in others.values() if ttype == "selective"]
                if selective_sequences and trigger.sequence <= max(selective_sequences):
                    still_remaining.append(trigger)
                    continue
            simulated[trigger.trigger_key] = (trigger.trigger_type, trigger.sequence)
            ordered.append(trigger)
            progressed = True
        remaining = still_remaining

    if remaining:
        stuck_keys = sorted(trigger.trigger_key for trigger in remaining)
        raise LockoutPlanDivergedError(
            f"SS round {ss_round_id} cannot be synchronised automatically: trigger key(s) {stuck_keys} form a "
            "sequence-reordering cycle (e.g. two triggers swapping sequences) that cannot be applied one trigger "
            "at a time without a transient collision. Reorder them manually (e.g. via a temporary intermediate "
            "sequence) before retrying synchronisation."
        )
    return ordered


def _validate_trigger_sync_plan(
    ss_round_id, finals_triggers, ss_triggers_by_key, activated_ss_trigger_ids, finals_removed_keys=frozenset()
):
    """The trigger half of `synchronise_lockout_plan_from_finals`'s
    validation -- computes what would need to change and a safe write
    order for it, entirely from already-fetched data: no database access
    of its own, no mutation of any kind. Issue #211 P2 (Codex review,
    round 3): called *before* `confirm_afl_mapping` mutates SS's mapping,
    so a `LockoutPlanDivergedError` raised here (an obsolete SS-only
    trigger key, an already-activated trigger that still needs changing,
    or a genuine sequence-reordering/ordering-rule cycle) leaves nothing
    at all mutated yet -- previously this validation only ran *after* the
    mapping had already been committed, silently violating that same
    error's own documented nothing-mutated contract whenever the mapping
    itself also needed correcting.

    `activated_ss_trigger_ids` is the caller's pre-fetched set of SS
    `trigger_id`s that have already durably activated (`bbbffl_round_
    lockout_trigger_activation`) -- passed in rather than queried here so
    this function stays pure/no-I/O. Issue #211 P1 (Codex review, round
    4): `LockoutTriggerRepository.configure()` permanently refuses to
    revise an activated trigger (`TriggerAlreadyActivatedError`), checked
    only at write time -- without checking this up front too, a plan with
    several pending changes could commit an earlier, still-editable
    trigger before discovering a later one has already irreversibly
    locked, again leaving SS half-synchronised despite this function's
    fail-closed contract.

    `finals_removed_keys` (issue #219) is the caller's pre-fetched set of
    trigger keys that exist on the finals side but have been explicitly
    removed there (`LockoutTriggerRepository.remove`) -- distinct from a
    key finals never had at all. An SS trigger key absent from `finals_
    triggers` (already active-only) *and* present in `finals_removed_keys`
    is mirrored onto SS as a removal (`keys_to_remove`, returned below)
    rather than raising: this is exactly what lets a Scorer's pre-
    activation Finals correction (e.g. dropping a mistaken `early-1`)
    propagate onto the concurrent SS round automatically, per issue #219's
    "Finals remains authoritative ... existing synchronisation" requirement,
    without reopening the unrelated "SS carries a trigger key finals never
    had" case, which still fails closed exactly as before."""
    finals_active_keys = {t.trigger_key for t in finals_triggers}
    obsolete_keys = set(ss_triggers_by_key) - finals_active_keys
    keys_to_remove = sorted(obsolete_keys & set(finals_removed_keys))
    truly_obsolete_keys = sorted(obsolete_keys - set(finals_removed_keys))
    if truly_obsolete_keys:
        raise LockoutPlanDivergedError(
            f"SS round {ss_round_id} has lockout trigger key(s) {truly_obsolete_keys} that no longer exist in the "
            "concurrent finals week's current plan (and were never removed there either). Reconcile it directly "
            "(e.g. repoint it via the round-preflight lockout form) before synchronisation can proceed."
        )
    activated_removal_keys = sorted(
        key for key in keys_to_remove if ss_triggers_by_key[key].trigger_id in activated_ss_trigger_ids
    )
    if activated_removal_keys:
        raise LockoutPlanDivergedError(
            f"SS round {ss_round_id} cannot be synchronised automatically: trigger key(s) {activated_removal_keys} "
            "were removed from the concurrent finals plan, but have already activated (irreversibly locked) on "
            "SS -- an activated trigger can never be removed. Reconcile this divergence directly before "
            "synchronisation can proceed."
        )

    pending_keys = set()
    unchanged_trigger_keys = []
    for trigger in finals_triggers:
        existing = ss_triggers_by_key.get(trigger.trigger_key)
        desired = (trigger.trigger_type, trigger.sequence, tuple(sorted(trigger.afl_match_ids)))
        current = (
            (existing.trigger_type, existing.sequence, tuple(sorted(existing.afl_match_ids)))
            if existing is not None
            else None
        )
        if current == desired:
            unchanged_trigger_keys.append(trigger.trigger_key)
        else:
            pending_keys.add(trigger.trigger_key)

    activated_pending_keys = sorted(
        key
        for key in pending_keys
        if ss_triggers_by_key.get(key) is not None and ss_triggers_by_key[key].trigger_id in activated_ss_trigger_ids
    )
    if activated_pending_keys:
        raise LockoutPlanDivergedError(
            f"SS round {ss_round_id} cannot be synchronised automatically: trigger key(s) {activated_pending_keys} "
            "have already activated (irreversibly locked) on SS, but the concurrent finals plan now differs for "
            "them. LockoutTriggerRepository.configure permanently refuses to revise an activated trigger -- "
            "reconcile this divergence directly before synchronisation can proceed."
        )

    # Plan every pending trigger's safe application order *before* writing
    # anything -- see `_plan_trigger_sync`'s own docstring for why this
    # must happen up front rather than interleaved with the writes below.
    # `keys_to_remove` are excluded from the simulated SS state here --
    # they are applied (see `_apply_trigger_sync`'s sibling below) before
    # any configure-sync write, so their sequences are genuinely free for a
    # pending trigger to reuse by the time this plan actually runs, not
    # still "occupied" by a trigger about to disappear.
    ordered_plan = _plan_trigger_sync(
        ss_round_id,
        [t for t in finals_triggers if t.trigger_key in pending_keys],
        {key: value for key, value in ss_triggers_by_key.items() if key not in keys_to_remove},
    )
    return ordered_plan, unchanged_trigger_keys, keys_to_remove


def _apply_trigger_sync(
    conn,
    trigger_repo,
    ss_round_id,
    ordered_plan,
    ss_triggers_including_removed_by_key,
    ss_mapping_revision,
    actor,
    reason,
):
    """Applies an already-validated `_validate_trigger_sync_plan` order as
    real writes, all against `conn` -- an already-open transaction the
    caller (`_synchronise_triggers_locked`) has locked and read/validated
    within, so every write here shares that one transaction rather than
    `configure()`'s own independent-per-call one (issue #211 P1, Codex
    review, round 6: a `TriggerAlreadyActivatedError` raised partway
    through by `_configure_locked`'s own at-write-time check now rolls
    every write this call already made back too, via that shared
    transaction -- round 5 first tried translating that error while
    leaving earlier writes independently committed, which round 6
    correctly rejected: the newly-activated trigger's configuration is
    now *permanently* frozen and can never converge with finals' current
    plan, so that "partial" result was not actually recoverable by
    retrying, only by an operator reconciling the divergence directly).

    `ss_triggers_including_removed_by_key` (issue #219, Codex review,
    PR #220, P1, round 2) -- unlike `ss_triggers_by_key` elsewhere in this
    module, which is deliberately active-only for obsolete/pending/
    ordering decisions -- includes a removed SS trigger too, so
    `expected_revision` here always reflects a key's *real* current row
    (whether active or removed), never a value that collapses "removed"
    to the same 0 a truly-never-created key would also present (see
    `_configure_locked`'s own docstring for why that distinction matters).
    Safe to resolve this way specifically because it is read fresh, inside
    the same already-locked transaction as this write -- never a separate,
    racy round-trip the way an interactive HTTP submission would be."""
    synced_trigger_keys: list[str] = []
    for trigger in ordered_plan:
        existing = ss_triggers_including_removed_by_key.get(trigger.trigger_key)
        trigger_repo._configure_locked(
            conn,
            ss_round_id,
            trigger.trigger_key,
            trigger.trigger_type,
            trigger.sequence,
            tuple(trigger.afl_match_ids),
            actor=actor,
            reason=reason,
            expected_revision=existing.revision if existing is not None else 0,
            expected_mapping_revision=ss_mapping_revision,
        )
        synced_trigger_keys.append(trigger.trigger_key)
    return synced_trigger_keys


def _apply_trigger_removals(conn, trigger_repo, ss_round_id, keys_to_remove, ss_triggers_by_key, actor, reason):
    """Issue #219: mirrors onto SS every trigger key `_validate_trigger_
    sync_plan` determined was removed from the concurrent finals plan --
    against the same already-locked `conn` `_apply_trigger_sync` writes
    into, so a `TriggerAlreadyActivatedError` discovered here (a concurrent
    activation between validation and this write) rolls back everything
    this synchronisation call has done, exactly like `_apply_trigger_sync`
    itself. Applied *before* `_apply_trigger_sync` -- a removal only ever
    frees a sequence, never conflicts with one, so ordering it first is
    always safe and is what lets a pending configure change reuse a
    just-freed sequence in the same synchronisation call.

    Passes each key's already-read revision through as `expected_revision`
    -- mirroring `_apply_trigger_sync`'s identical use of `existing.
    revision` -- so a genuinely concurrent change to SS's own trigger
    between this transaction's own read and this write (not otherwise
    possible once the round-row lock `_remove_locked` now takes is held,
    but kept for the same defence-in-depth reason `_apply_trigger_sync`
    already carries it) surfaces as `StaleTriggerRevisionError` rather than
    silently applying against a superseded row."""
    removed_trigger_keys: list[str] = []
    for trigger_key in keys_to_remove:
        trigger_repo._remove_locked(
            conn,
            ss_round_id,
            trigger_key,
            actor=actor,
            reason=reason,
            expected_revision=ss_triggers_by_key[trigger_key].revision,
        )
        removed_trigger_keys.append(trigger_key)
    return removed_trigger_keys


def _synchronise_triggers_locked(
    database, trigger_repo, ss_round_id, finals_round_id, ss_mapping_revision, actor, reason
):
    """The actual, race-safe trigger synchronisation: reads SS's and
    finals' current trigger sets, validates/plans, and writes -- all
    under one transaction that locks *both* round rows first, before any
    of those reads. Issue #211 P1 (Codex review, round 7): the caller
    (`synchronise_lockout_plan_from_finals`) also runs an unlocked
    pre-check, purely as a fast-fail for the ordinary, uncontended case
    so an already-broken plan never wastes a mapping mutation (issue #211
    P2, Codex review, round 3) -- but that pre-check's read is not itself
    protected by any lock, so a genuinely concurrent trigger change (a
    different operator's `configure()` call, or one inserting a brand-new
    SS-only trigger) landing in the narrow window right after it returns
    would otherwise go undetected: `ordered_plan` would still reflect the
    stale snapshot, and the actual writes below would silently miss it.
    Any concurrent `configure()`/`_configure_locked` call for either round
    must itself acquire that exact round's own row lock first (see
    `LockoutTriggerRepository.configure`'s own docstring), so once this
    transaction holds both, nothing else can change either trigger set
    this function reads until this transaction ends -- this is the read
    that actually decides what gets written, never the caller's own
    earlier, merely-advisory one.

    Issue #211 P1 (Codex review, round 8): round 7 locked only SS's round
    -- a scorer revising a *finals* trigger (via `app.round_preflight.
    configure_preflight_trigger`, itself layered over this same
    `LockoutTriggerRepository.configure`) immediately after this read
    could still leave this transaction copying an already-stale finals
    snapshot onto SS. Locking finals' round row too, in a fixed order
    (finals, then SS -- nothing else in this codebase locks both a finals
    and a SuperScore round together, so picking one order and always
    using it is what keeps this from ever deadlocking against itself),
    closes that the same way the SS-side lock already closes the
    SS-side one.

    A concurrent trigger *activation* (`app.lockouts`'s own
    `_materialize_round_triggers`, which locks only one trigger's own
    header row, not either round-level one) discovered while writing is
    still handled exactly as `_apply_trigger_sync`'s own docstring
    describes: the whole transaction rolls back together."""
    try:
        with transaction(database) as conn:
            conn.execute(
                "SELECT 1 FROM bbbffl_round WHERE bbbffl_round_id=?" + _for_update_suffix(database),
                (finals_round_id,),
            )
            conn.execute(
                "SELECT 1 FROM bbbffl_round WHERE bbbffl_round_id=?" + _for_update_suffix(database),
                (ss_round_id,),
            )
            finals_triggers = trigger_repo.list_triggers(finals_round_id)
            # Issue #219: `include_removed=True` here (unlike every other
            # `list_triggers` call in this module) is deliberate -- this is
            # the one place that needs to distinguish "finals never had
            # this key" from "finals had it and explicitly removed it",
            # exactly the distinction `_validate_trigger_sync_plan` uses to
            # decide whether an obsolete SS key is safe to auto-remove.
            finals_removed_keys = {
                t.trigger_key
                for t in trigger_repo.list_triggers(finals_round_id, include_removed=True)
                if t.removed_at is not None
            }
            ss_triggers_by_key = {t.trigger_key: t for t in trigger_repo.list_triggers(ss_round_id)}
            # Issue #219, Codex review (PR #220, P1, round 2): a *second*
            # read, still inside this same lock, that also includes a
            # removed SS trigger -- `_apply_trigger_sync` needs each
            # pending key's *real* current row (active or removed) to
            # compute a correct `expected_revision`, never the same 0 a
            # truly-never-created key would also present (see
            # `_configure_locked`'s own docstring). `ss_triggers_by_key`
            # itself stays active-only -- every other decision here
            # (obsolete/pending/unchanged/ordering) is correctly scoped to
            # the round's *active* plan only.
            ss_triggers_including_removed_by_key = {
                t.trigger_key: t for t in trigger_repo.list_triggers(ss_round_id, include_removed=True)
            }
            ss_trigger_ids = [t.trigger_id for t in ss_triggers_by_key.values()]
            activated_ss_trigger_ids = (
                {
                    row["trigger_id"]
                    for row in conn.execute(
                        "SELECT trigger_id FROM bbbffl_round_lockout_trigger_activation "
                        f"WHERE trigger_id IN ({','.join('?' * len(ss_trigger_ids))})",
                        tuple(ss_trigger_ids),
                    ).fetchall()
                }
                if ss_trigger_ids
                else set()
            )
            ordered_plan, unchanged_trigger_keys, keys_to_remove = _validate_trigger_sync_plan(
                ss_round_id, finals_triggers, ss_triggers_by_key, activated_ss_trigger_ids, finals_removed_keys
            )
            removed_trigger_keys = _apply_trigger_removals(
                conn, trigger_repo, ss_round_id, keys_to_remove, ss_triggers_by_key, actor, reason
            )
            synced_trigger_keys = _apply_trigger_sync(
                conn,
                trigger_repo,
                ss_round_id,
                ordered_plan,
                ss_triggers_including_removed_by_key,
                ss_mapping_revision,
                actor,
                reason,
            )
    except (TriggerAlreadyActivatedError, TriggerAlreadyRemovedError) as exc:
        raise LockoutPlanDivergedError(
            f"SS round {ss_round_id} cannot be synchronised automatically: a trigger activated or was removed "
            "concurrently, between this synchronisation's own validation and its writes -- nothing from this "
            f"synchronisation attempt was applied (rolled back together). Reconcile the resulting divergence "
            f"directly. ({exc})"
        ) from exc
    return synced_trigger_keys, unchanged_trigger_keys, removed_trigger_keys


def synchronise_lockout_plan_from_finals(
    database,
    validator: AflReferenceValidator,
    ss_round_id: str,
    *,
    actor: ActorContext = ActorContext.anonymous_operator("admin"),
    reason: str | None = None,
) -> dict:
    """See this module's own docstring ("Workflow A") for the full
    rationale. Returns a summary dict (`ss_round_id`, `finals_round_id`,
    `mapping_synced`, `synced_trigger_keys`, `unchanged_trigger_keys`,
    `changed`)."""
    finals_mapping = resolve_concurrent_finals_afl_mapping(database, ss_round_id)
    finals_round_id = finals_mapping.bbbffl_round_id
    default_reason = reason or (f"SS lockout plan synchronised from concurrent finals round {finals_round_id}")

    # Issue #211 P1 (Codex review, round 2): once SS's round has been set
    # up (`app.superscore_round.setup_round` -> `create_non_ordinary_round`),
    # its AFL mapping is frozen onto its own `bbbffl_round_lifecycle` row --
    # see `FrozenMappingDivergedError`'s own docstring for why correcting
    # only the mutable mapping head below would silently misreport success
    # while calculations kept consuming the stale frozen snapshot. Checked
    # before any mutation, so a divergence here mutates nothing.
    frozen_round = CompetitionLifecycleRepository(database).get_round(ss_round_id)
    if frozen_round is not None and (
        frozen_round.afl_season_id != finals_mapping.afl_season_id
        or frozen_round.afl_round_id != finals_mapping.afl_round_id
    ):
        raise FrozenMappingDivergedError(
            f"SS round {ss_round_id} already has a frozen AFL mapping (season {frozen_round.afl_season_id}, "
            f"round {frozen_round.afl_round_id}) that diverges from the concurrent finals week's current mapping "
            f"(season {finals_mapping.afl_season_id}, round {finals_mapping.afl_round_id}). Correcting only the "
            "mutable mapping head would not update the frozen snapshot calculations actually use -- resolve this "
            "divergence directly before synchronisation can proceed."
        )

    existing_ss_mapping = RoundMappingRepository(database).resolve(ss_round_id)
    mapping_synced = existing_ss_mapping is None or (
        existing_ss_mapping.afl_season_id != finals_mapping.afl_season_id
        or existing_ss_mapping.afl_round_id != finals_mapping.afl_round_id
    )

    # Issue #211 P2 (Codex review, round 3): an unlocked pre-check of the
    # *entire* trigger plan -- including the obsolete-key/cycle/activation
    # checks -- before mutating the mapping below, purely as a fast-fail
    # for the ordinary, uncontended case: raising `LockoutPlanDivergedError`
    # only after `confirm_afl_mapping` had already committed left the
    # mapping silently advanced despite that error's own nothing-mutated
    # contract. This read is not itself protected by any lock, though --
    # see `_synchronise_triggers_locked`'s own docstring (issue #211 P1,
    # Codex review, round 7) for why the real, race-safe decision is the
    # fresh, locked re-read/re-validation that function performs, never
    # this one.
    trigger_repo = LockoutTriggerRepository(database)
    finals_triggers = trigger_repo.list_triggers(finals_round_id)
    finals_removed_keys = {
        t.trigger_key
        for t in trigger_repo.list_triggers(finals_round_id, include_removed=True)
        if t.removed_at is not None
    }
    ss_triggers_by_key = {t.trigger_key: t for t in trigger_repo.list_triggers(ss_round_id)}
    ss_trigger_ids = [t.trigger_id for t in ss_triggers_by_key.values()]
    activated_ss_trigger_ids = (
        {
            row["trigger_id"]
            for row in database.execute(
                "SELECT trigger_id FROM bbbffl_round_lockout_trigger_activation "
                f"WHERE trigger_id IN ({','.join('?' * len(ss_trigger_ids))})",
                tuple(ss_trigger_ids),
            ).fetchall()
        }
        if ss_trigger_ids
        else set()
    )
    _validate_trigger_sync_plan(
        ss_round_id, finals_triggers, ss_triggers_by_key, activated_ss_trigger_ids, finals_removed_keys
    )

    ss_mapping = confirm_afl_mapping(
        database,
        validator,
        ss_round_id,
        finals_mapping.afl_season_id,
        finals_mapping.afl_round_id,
        actor=actor,
        reason=default_reason,
    )

    synced_trigger_keys, unchanged_trigger_keys, removed_trigger_keys = _synchronise_triggers_locked(
        database, trigger_repo, ss_round_id, finals_round_id, ss_mapping.revision, actor, default_reason
    )

    changed = mapping_synced or bool(synced_trigger_keys) or bool(removed_trigger_keys)
    with transaction(database) as conn:
        append_event(
            conn,
            actor=actor,
            action="superscore.lockout_plan.synchronised_from_finals",
            entity_type="superscore.round",
            entity_id=ss_round_id,
            entity_version=str(len(synced_trigger_keys)),
            reason=default_reason,
            after_state={
                "finals_round_id": finals_round_id,
                "synced_trigger_keys": synced_trigger_keys,
                "unchanged_trigger_keys": unchanged_trigger_keys,
                "removed_trigger_keys": removed_trigger_keys,
                "mapping_synced": mapping_synced,
            },
            payload={"changed": changed},
        )
    return {
        "ss_round_id": ss_round_id,
        "finals_round_id": finals_round_id,
        "mapping_synced": mapping_synced,
        "synced_trigger_keys": synced_trigger_keys,
        "unchanged_trigger_keys": unchanged_trigger_keys,
        "removed_trigger_keys": removed_trigger_keys,
        "changed": changed,
    }


def _resolve_superscore_round_id(database, season_id: str, week_number: int) -> str | None:
    row = database.execute(
        "SELECT sr.bbbffl_round_id FROM bbbffl_round sr "
        "JOIN competition_stream sc ON sc.competition_id=sr.competition_id "
        "WHERE sc.season_id=? AND sc.stream_type='superscore' AND sr.round_key=?",
        (season_id, f"ss{week_number}"),
    ).fetchone()
    return row["bbbffl_round_id"] if row else None


def open_finals_and_superscore_week(
    database,
    afl_client,
    bracket_id: str,
    week_number: int,
    *,
    actor,
    reason: str | None = None,
) -> dict:
    bracket_repo = FinalsBracketRepository(database)
    bracket = bracket_repo.get_bracket_by_id(bracket_id)
    if bracket is None:
        raise PairedOpenWeekError(f"unknown finals bracket {bracket_id}")
    finals_round_id = bracket_repo.get_week_round_id(bracket_id, week_number)
    superscore_round_id = _resolve_superscore_round_id(database, bracket.season_id, week_number)
    if superscore_round_id is None:
        raise PairedOpenWeekError(
            f"no SuperScore round is configured for week {week_number} of season {bracket.season_id}; "
            "cannot pair-open with the finals week"
        )

    lifecycle = CompetitionLifecycleRepository(database)
    # Issue #211 P2 (Codex review): a lifecycle row existing is not the
    # same as being open -- both `app.finals.FinalsBracketRepository.
    # open_finals_week` and `app.superscore_round.setup_round` create their
    # `bbbffl_round_lifecycle` row in `upcoming` state *before* any open
    # transition (SS's `setup_round` never opens the round itself at all;
    # only `open_round` does). Treating a merely-`upcoming` row as "already
    # open" would silently skip the actual open transition and leave that
    # half stuck at `upcoming` forever through this action.
    finals_current = lifecycle.get_round(finals_round_id)
    finals_already_open = finals_current is not None and finals_current.state != "upcoming"
    superscore_current = lifecycle.get_round(superscore_round_id)
    superscore_already_open = superscore_current is not None and superscore_current.state != "upcoming"

    if not finals_already_open:
        finals_preflight = build_finals_week_preflight(database, bracket_id, week_number)
        if not finals_preflight["readiness"]["safe_to_open"]:
            raise PairedOpenWeekError(
                f"finals week {week_number} failed preflight and the pairing was not opened: "
                f"{finals_preflight['readiness']['blockers']}"
            )

    if not superscore_already_open:
        # Issue #211 P1 (Codex review, round 2): this check must gate on
        # whether *SuperScore* is about to open, not on whether Finals
        # itself is already open -- `build_finals_week_preflight` reports
        # "safe to open" purely from mapping/pairing state, it never
        # requires a configured lockout plan (unlike the ordinary round
        # preflight's own `main_lockout_incomplete` blocker), and Finals
        # can already be open via the still-supported standalone `/open`
        # endpoint with no main trigger configured at all. Nesting this
        # under `not finals_already_open` (as it was before) let exactly
        # that case bypass it entirely: retrying the paired action would
        # synchronise the finals plan's absent main trigger onto SS and
        # still open it, leaving both streams' selections without a round
        # lockout.
        finals_triggers = LockoutTriggerRepository(database).list_triggers(finals_round_id)
        if not any(t.trigger_type == "main" for t in finals_triggers):
            raise PairedOpenWeekError(
                f"finals week {week_number} has no configured main/remaining lockout trigger; configure the "
                "finals lockout plan before opening this pairing."
            )

        # Issue #211 P1 (Codex review, round 4): a main trigger merely
        # *existing* is not enough -- the supported pre-open mapping-
        # correction path (`app.round_preflight.accept_preflight_mapping`)
        # can move finals onto a different AFL round while leaving its
        # already-configured triggers' `afl_match_ids` unchanged, and
        # neither `build_finals_week_preflight` nor `LockoutTriggerRepository
        # .configure` validate a trigger's match IDs against the mapped
        # round's real evidence. A trigger referencing match IDs outside
        # the round it's actually mapped to can never activate -- silently
        # leaving both streams' selections without a working lockout, not
        # merely finals'. Validated against the exact AFL round finals is
        # (or is about to be) mapped onto, mirroring `resolve_concurrent_
        # finals_afl_mapping`'s own frozen-vs-head distinction.
        if finals_already_open:
            frozen_finals = database.execute(
                "SELECT afl_round_id FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?", (finals_round_id,)
            ).fetchone()
            coverage_afl_round_id = frozen_finals["afl_round_id"] if frozen_finals is not None else None
        else:
            coverage_mapping = RoundMappingRepository(database).resolve(finals_round_id)
            coverage_afl_round_id = coverage_mapping.afl_round_id if coverage_mapping is not None else None
        if coverage_afl_round_id is None:
            raise PairedOpenWeekError(
                f"finals week {week_number} has no accepted AFL-round mapping yet; accept one before opening "
                "this pairing."
            )
        # Issue #211 P1 (Codex review, round 5): a resilient production
        # `afl_client` can serve this `get_matches` call from its own
        # last-known-good cache during a live AFL-API outage, returning
        # successfully rather than raising -- validating coverage against
        # that stale a list could approve trigger match IDs no longer
        # actually in the mapped round's fixture. Wrapped in the same
        # `evidence_batch()`/`is_evidence_fresh()` freshness scope
        # `app.round_preflight.configure_preflight_trigger` already uses
        # for its own membership check, rejecting a stale read outright.
        evidence_batch = getattr(afl_client, "evidence_batch", None)
        scope = evidence_batch() if callable(evidence_batch) else nullcontext(afl_client)
        with scope as evidence:
            try:
                matches = afl_client.get_matches(coverage_afl_round_id)
            except AflApiError as exc:
                raise PairedOpenWeekError(
                    f"could not verify finals week {week_number}'s lockout trigger match coverage against AFL "
                    f"round {coverage_afl_round_id}: {exc}"
                ) from exc
            freshness = getattr(evidence, "is_evidence_fresh", None)
            fresh = freshness() if callable(freshness) else True
        if not fresh:
            raise PairedOpenWeekError(
                f"finals week {week_number}'s mapped AFL match evidence is being served from a stale cache; "
                "refresh live evidence before opening this pairing."
            )
        valid_match_ids = {match.match_id for match in matches}
        stale_trigger_keys = sorted(
            t.trigger_key for t in finals_triggers if not set(t.afl_match_ids) <= valid_match_ids
        )
        if stale_trigger_keys:
            raise PairedOpenWeekError(
                f"finals week {week_number} has lockout trigger(s) {stale_trigger_keys} whose configured AFL match "
                f"IDs are not part of AFL round {coverage_afl_round_id}'s current match list -- they can never "
                "activate against the currently mapped round; reconfigure the finals lockout plan before opening "
                "this pairing."
            )

    default_reason = reason or (
        f"Paired Open week action for finals week {week_number} and its concurrent SuperScore round"
    )

    if not finals_already_open:
        # `open_finals_week` returns `{"round": ..., "already_open": bool}`,
        # not the lifecycle row directly -- re-reading it below keeps this
        # function's own return shape identical whichever branch ran.
        open_finals_week(database, bracket_id, week_number, actor=actor, reason=default_reason)
    finals_round = lifecycle.get_round(finals_round_id)

    validator = AflApiReferenceValidator(afl_client)
    sync_result = synchronise_lockout_plan_from_finals(
        database, validator, superscore_round_id, actor=actor, reason=default_reason
    )

    if superscore_already_open:
        superscore_round = lifecycle.get_round(superscore_round_id)
    else:
        setup_round(database, superscore_round_id, actor=actor, reason=default_reason)
        superscore_round = open_round(database, superscore_round_id, actor=actor, reason=default_reason)

    return {
        "bracket_id": bracket_id,
        "week_number": week_number,
        "finals_round_id": finals_round_id,
        "finals_state": finals_round.state,
        "finals_already_open": finals_already_open,
        "superscore_round_id": superscore_round_id,
        "superscore_state": superscore_round.state,
        "superscore_already_open": superscore_already_open,
        "lockout_sync": sync_result,
    }
