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
"""

from app.audit import ActorContext, append_event
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import transaction
from app.finals import FinalsBracketRepository
from app.finals_preflight import build_finals_week_preflight, open_finals_week
from app.lockouts import LockoutTriggerRepository
from app.round_mapping import AflApiReferenceValidator, AflReferenceValidator, RoundMappingRepository
from app.superscore_round import confirm_afl_mapping, open_round, resolve_concurrent_finals_afl_mapping, setup_round

__all__ = [
    "LockoutPlanDivergedError",
    "PairedOpenWeekError",
    "open_finals_and_superscore_week",
    "synchronise_lockout_plan_from_finals",
]


class PairedOpenWeekError(Exception):
    """The finals/SuperScore pairing failed validation -- nothing was
    opened and nothing was mutated."""


class LockoutPlanDivergedError(Exception):
    """SS's persisted lockout-trigger plan cannot be safely/automatically
    reconciled with the finals week's current one -- nothing was mutated;
    an operator must resolve the divergence directly (e.g. via
    `app.lockouts.LockoutTriggerRepository`/the round-preflight lockout
    form) before synchronisation can proceed. Raised for two distinct
    reasons (see `synchronise_lockout_plan_from_finals`): an obsolete SS
    trigger key the finals plan no longer has (the repository has no
    delete/deactivate primitive to reconcile it automatically), or a
    genuine sequence-reordering cycle (e.g. two trigger keys swapping
    sequences) that cannot be applied one trigger at a time without a
    transient collision."""


def _sync_triggers(trigger_repo, ss_round_id, finals_triggers, ss_triggers_by_key, ss_mapping_revision, actor, reason):
    """The trigger half of `synchronise_lockout_plan_from_finals` -- see
    that function's docstring and this module's own for the full
    rationale. Issue #211 P1 (Codex review): fails closed, mutating
    nothing, rather than silently leaving a stale SS-only trigger active
    or attempting a reorder that could transiently violate
    `LockoutTriggerRepository.configure`'s own sequence-uniqueness/
    ordering rules."""
    obsolete_keys = sorted(set(ss_triggers_by_key) - {t.trigger_key for t in finals_triggers})
    if obsolete_keys:
        raise LockoutPlanDivergedError(
            f"SS round {ss_round_id} has lockout trigger key(s) {obsolete_keys} that no longer exist in the "
            "concurrent finals week's current plan. LockoutTriggerRepository has no delete/deactivate primitive, "
            "so a stale trigger can never be silently dropped here -- reconcile it directly (e.g. repoint it via "
            "the round-preflight lockout form) before synchronisation can proceed."
        )

    pending = []
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
            pending.append((trigger, existing))

    # Issue #211 P1 (Codex review): applying every changed trigger's real
    # target sequence directly, in an arbitrary order, cannot safely
    # express a *reordering* -- `configure()`'s sequence-uniqueness check
    # always compares against every currently-persisted trigger for the
    # round, so e.g. swapping two sequences can transiently collide with a
    # not-yet-updated sibling's still-old sequence. A greedy multi-pass
    # application instead only ever applies a change once its target
    # sequence is free against SS's *current* persisted state -- correct
    # for the common cases Codex called out (a new early trigger inserted,
    # shifting the existing main/selective stages) without needing any
    # fragile temporary-value manoeuvre. A genuine cycle (two keys
    # swapping sequences with each other) can never converge this way --
    # detected below and reported, never silently left half-applied.
    synced_trigger_keys: list[str] = []
    progressed = True
    while pending and progressed:
        progressed = False
        still_pending = []
        current_sequences_by_key = {t.trigger_key: t.sequence for t in trigger_repo.list_triggers(ss_round_id)}
        for trigger, existing in pending:
            occupied = {seq for key, seq in current_sequences_by_key.items() if key != trigger.trigger_key}
            if trigger.sequence in occupied:
                still_pending.append((trigger, existing))
                continue
            trigger_repo.configure(
                ss_round_id,
                trigger.trigger_key,
                trigger.trigger_type,
                trigger.sequence,
                list(trigger.afl_match_ids),
                actor=actor,
                reason=reason,
                expected_revision=existing.revision if existing is not None else 0,
                expected_mapping_revision=ss_mapping_revision,
            )
            synced_trigger_keys.append(trigger.trigger_key)
            progressed = True
        pending = still_pending

    if pending:
        stuck_keys = sorted(trigger.trigger_key for trigger, _existing in pending)
        raise LockoutPlanDivergedError(
            f"SS round {ss_round_id} cannot be synchronised automatically: trigger key(s) {stuck_keys} form a "
            "sequence-reordering cycle (e.g. two triggers swapping sequences) that cannot be applied one trigger "
            "at a time without a transient collision. Reorder them manually (e.g. via a temporary intermediate "
            "sequence) before retrying synchronisation."
        )

    return synced_trigger_keys, unchanged_trigger_keys


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
    existing_ss_mapping = RoundMappingRepository(database).resolve(ss_round_id)
    mapping_synced = existing_ss_mapping is None or (
        existing_ss_mapping.afl_season_id != finals_mapping.afl_season_id
        or existing_ss_mapping.afl_round_id != finals_mapping.afl_round_id
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

    trigger_repo = LockoutTriggerRepository(database)
    finals_triggers = trigger_repo.list_triggers(finals_round_id)
    ss_triggers_by_key = {t.trigger_key: t for t in trigger_repo.list_triggers(ss_round_id)}

    synced_trigger_keys, unchanged_trigger_keys = _sync_triggers(
        trigger_repo, ss_round_id, finals_triggers, ss_triggers_by_key, ss_mapping.revision, actor, default_reason
    )

    changed = mapping_synced or bool(synced_trigger_keys)
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
        # Issue #211 P1 (Codex review): `build_finals_week_preflight`
        # reports "safe to open" purely from mapping/pairing state -- it
        # never requires a configured lockout plan (unlike the ordinary
        # round preflight's own `main_lockout_incomplete` blocker). Opening
        # this pairing with no configured main trigger would synchronise
        # that same absence onto SS, silently leaving both streams'
        # selections without a round lockout at all.
        finals_triggers = LockoutTriggerRepository(database).list_triggers(finals_round_id)
        if not any(t.trigger_type == "main" for t in finals_triggers):
            raise PairedOpenWeekError(
                f"finals week {week_number} has no configured main/remaining lockout trigger; configure the "
                "finals lockout plan before opening this pairing."
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
