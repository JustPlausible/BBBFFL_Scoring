# Staged AFL-match lockouts

**Status:** implemented, work package 23 (issue #34, revised per maintainer
follow-up on PR #45)<br>
**Implementation:** `bbbffl_app/app/lockouts.py`, integrated into
`bbbffl_app/app/lineups.py`'s `WeeklyLineupRepository.submit`<br>
**Schema:** `bbbffl_app/migrations/versions/0012_lockouts.py` (per-position
lock evidence) and `0013_lockout_triggers.py` (the round lockout plan and
its activation evidence), see [`database-migrations.md`](database-migrations.md)<br>
**Depends on:** the validated `afl-api` v1 boundary
([`afl-api-v1-contract.md`](afl-api-v1-contract.md), issue #18), the accepted
BBBFFL-to-AFL round mapping ([`round-afl-mapping.md`](round-afl-mapping.md),
issue #31), the persisted round/matchup lifecycle
([`competition-lifecycle.md`](competition-lifecycle.md), issue #32), and
weekly lineup drafts/submissions ([`weekly-lineups.md`](weekly-lineups.md),
issue #33).

## Purpose

A BBBFFL round does not lock as a whole when the first AFL match begins, and
it does not lock a player merely because *that player's own* AFL match has
started. Instead, **BBBFFL chooses, per round, which AFL matches actually
cause a lockout** -- a persisted lockout plan of commissioner/scorer-
configured triggers -- and each selected player locks according to whichever
configured trigger, if any, covers their match. `afl-api` supplies match
identity, scheduled start and status; it never decides which matches matter
for BBBFFL lockout purposes.

## The round lockout plan

`app.lockouts.LockoutTriggerRepository` persists an ordered set of
**triggers** per BBBFFL round:

- **selective** ("early lockout") triggers: each associated with one or more
  specific AFL match IDs (stable IDs, never team/match names). A selective
  trigger activates the instant *any* of its associated matches reaches its
  own lock boundary; once activated, every selected player whose own
  resolved match is in that trigger's match set locks -- even if their
  particular match, if different from the one that actually fired the
  trigger, has not itself started. This generalises the season model's "once
  an early AFL match starts, any selected player from either participating
  AFL club is locked" to a configured *group* of matches sharing one
  lockout moment (e.g. a Thursday double-header).
- **main**: the round's final trigger. Once activated, *every remaining
  editable position in every lineup for the round locks at once*, regardless
  of whether that player's own AFL match has itself started. A round has at
  most one current main trigger (`LockoutTriggerRepository` enforces this).

A round may configure zero or more selective triggers plus one main trigger
-- the common case is "optional early stage(s), then main" -- but nothing is
hard-coded to exactly two stages. An unusual round (an extra Tuesday
fixture, a scheduling quirk) configures additional selective triggers with
no code change, only additional `LockoutTriggerRepository.create` calls;
each trigger's `sequence` gives a deterministic ordering for display.

**Critically: a player whose AFL match is not covered by any activated
trigger remains editable, even if that match has itself already started.**
An AFL match commencing does not, by itself, freeze anything -- only a
*configured* trigger does. (An earlier version of this module got this
backwards, treating every AFL match as its own independent trigger; that was
corrected following review on PR #45.) A round with no lockout plan
configured at all evaluates every position as `indeterminate` (`"lockout_plan_not_configured"`)
-- fail closed, never guessed, matching the season model's design
principle to never invent a lockout decision from AFL scheduling alone.

Trigger configuration is never inferred from AFL scheduling: not day of
week, not chronological match order, not "first match of the round", not
the AFL round number, not browser/UI state. It is always an explicit
commissioner/scorer decision (`create`/`replace`, both actor/reason-audited
via `app.audit.LOCKOUT_TRIGGER_CONFIGURED`). `LockoutTriggerRepository` is
the persistence/service boundary a later commissioner/scorer management UI
would call -- no UI is built in this issue.

## Player -> AFL match resolution

`app.lockouts.resolve_match(afl_team_id, matches)` matches a selected
player's cached `afl_team_id` (from `season_player_pool`, itself populated
from `afl-api`'s player identity endpoint) against the AFL club identities
embedded in the round's match list. This is a stable-ID join, never a
name-based one. `matches` is always the mapped round's match list from
`app.afl_client.AflApiClient.get_matches`, reached exclusively through the
accepted BBBFFL round mapping (`RoundMatchFactsProvider`, wrapping
`app.round_mapping.RoundMappingRepository.resolve` + the AFL client) --
never a provider "current round" or other implicit state. If zero or more
than one match involves the player's club, resolution fails explicitly
(`MatchResolutionError`) rather than guessing -- surfaced as an
`indeterminate` position, never a lock decision.

## The lock rule and where it lives

Two decisions, at two different granularities:

1. **Does a trigger's own associated match reach boundary?**
   `app.lockouts.evaluate_match_lock(match, evaluation_at)` -- a pure
   function, no I/O, no persisted evidence -- decides whether *one* AFL
   match has reached its own boundary:
   - **editable** while its status is `UPCOMING` (or a tolerated legacy
     alias) and `evaluation_at` is strictly before `start_time_utc`;
   - **locked** once `evaluation_at >= start_time_utc` (inclusive), or once
     `LIVE`, `POSTGAME` or `CONCLUDED` is observed regardless of time
     (status is authoritative over a stale/missing schedule; `POSTGAME` and
     `CONCLUDED` are never collapsed into one reason);
   - **indeterminate** when the status is not one of `afl-api`'s documented
     v1 lifecycle values/aliases (`app.afl_client.is_recognized_match_status`),
     or it is `UPCOMING` with no scheduled start time. A trigger whose only
     associated match is indeterminate simply does not activate yet.

   This is used only by `LockoutRepository._materialize_round_triggers` to
   decide **trigger activation** -- never directly as a per-player decision
   any more.

2. **Given the round's currently-activated triggers, is this player
   locked?** `LockoutRepository._evaluate_position` resolves the player's
   own match, then asks: is that match's ID covered by an activated
   selective trigger (`"selective_trigger_activated"`)? Else, has the main
   trigger activated (`"main_lockout_triggered"`)? Else: `editable`
   (`"not_yet_triggered"`) -- unless the round has no lockout plan at all
   (`"lockout_plan_not_configured"`, `indeterminate`) or the player's match
   cannot be resolved at all (`indeterminate`, the `MatchResolutionError`
   message).

Both functions never call `datetime.now()`; every caller supplies (or
defaults once, at the outer edge) an explicit `evaluation_at`.

## Deterministic evaluation

`LockoutRepository.lock_state(lineup_id, bbbffl_round_id, season_entry_id,
positions, *, match_facts, evaluation_at=...)` answers "what is locked
right now" for an arbitrary `{position: season_player_id}` map, given:

1. the persisted lineup (identified by `lineup_id`);
2. the round's persisted lockout plan (`LockoutTriggerRepository`'s tables);
3. the accepted BBBFFL -> AFL round mapping (via `match_facts`, a
   `RoundMatchFactsProvider` in production, a fixed fake in tests);
4. controlled AFL match timing/status facts (`match_facts.matches_for(...)`);
5. the supplied `evaluation_at`;
6. any already-persisted trigger-activation and per-position lock evidence.

Given the same six inputs, the same `LineupLockView` comes back every time
-- this is what makes 2026 replay deterministic: seed the same persisted
lineup, the same persisted lockout plan, the same fixed match-fact fixtures
and the same replay clock, and lock state reproduces exactly. Tests never
sleep, poll, or depend on a live `afl-api` connection.

## Historical lock irreversibility

Two independent, cooperating layers make "locked, permanently" actually
true rather than aspirational:

### 1. Trigger-level

Once a trigger has activated, that fact is durably recorded in
`bbbffl_round_lockout_trigger_activation` (PK `trigger_id`; immutable via
the same trigger-based enforcement as `weekly_lineup_submission`/
`weekly_lineup_lock`). `LockoutTriggerRepository.replace` then permanently
refuses to change that trigger's configuration
(`TriggerAlreadyActivatedError`), so the set of AFL matches an activated
trigger covers can never change afterwards, and a later upstream schedule/
status correction to one of those matches cannot un-fire it either --
activation evidence, once written, is never recomputed against fresh
`matches` data.

### 2. Player-level

Once a *specific selected player's* position has been observed as locked,
that observation is durably recorded in `weekly_lineup_lock` (PK
`(lineup_id, position)`), exactly as before this round-plan model was
introduced. `_evaluate_position` always prefers a matching persisted row
over a fresh recomputation. Two invariants make this durable rather than
aspirational:

1. **Only the effective submission can be materialized.**
   `_evaluate_position` only *writes* a row when its caller passes
   `materializable=True`. The only caller that does is
   `LockoutRepository._materialize_lineup`, which always derives the
   positions it evaluates from `weekly_lineup`/
   `weekly_lineup_submission_slot`'s own `effective_submission_version` --
   never from whatever a caller passes as a `positions` argument, which
   `lock_state` explicitly documents may be an unsubmitted private draft. A
   draft selection can therefore never occupy the one immutable evidence
   slot a position has ahead of the player actually, officially selected
   there; `lock_state` still reports a draft player's own live-evaluated
   state for display, just never writes it.
2. **An observation survives a rejected mutation.** `_materialize_round_triggers`
   and `_materialize_lineup` always run in their own standalone
   transactions, independent of whatever happens afterwards.
   `WeeklyLineupRepository.submit` calls `lock_guard.materialize(lineup_id)`
   *before* opening its own transaction (see
   [Interaction with #33](#interaction-with-33-submitted-versions) below),
   so a lock discovered while evaluating a submission that is then rejected
   is not lost when that submission's own transaction rolls back.
   `guard_transition` -- which runs *inside* that transaction -- never
   writes to either evidence table at all (`materializable=False`
   throughout), precisely because a write there could not survive the
   `LockedSelectionError` it is often about to raise. The one residual gap:
   a trigger that activates in the narrow window between `materialize()`
   returning and `guard_transition` running is still correctly *rejected*
   (live evaluation always governs the accept/reject decision), but its
   evidence is not durably written until the next observation -- an
   eventual-consistency gap, never a correctness one.

Before a trigger has activated, a legitimate reconfiguration (a different
associated match, a different type) *does* move the boundary -- there is
nothing to protect yet, so the new configuration simply governs the next
evaluation.

## Interaction with #33 submitted versions

`WeeklyLineupRepository.submit`'s optional `lock_guard` parameter is the
sole integration point. `LockoutRepository.guard(match_facts=...,
evaluation_at=...)` returns a `LockGuard` with two roles `submit` invokes at
two different points: `.materialize(lineup_id)`, called *before* `submit`
opens its own transaction (see
[Historical lock irreversibility](#historical-lock-irreversibility) above
for why -- it materializes both the round's trigger activations and the
lineup's own evidence, in that order, each its own transaction); and the
object itself as a callable, invoked *inside* `submit`'s existing
transaction -- after the previous effective submission's positions are
read, before the new version is written -- which may raise
`LockedSelectionError` to abort the whole submission. `submit` detects the
`.materialize` method via `hasattr` (any plain callable without one still
works, just skips that step); it never imports `app.lockouts`, keeping the
two modules decoupled through this small duck-typed contract.

The guard compares the previous effective submission's positions against
the proposed ones:

- any position whose evaluated state is `locked` or `indeterminate` must
  keep its previous player unchanged, or the submission is rejected --
  covering removal, replacement and repositioning/swapping of a locked
  player, since a swap changes *two* positions and either one being locked
  is enough to reject the whole attempt;
- any position introducing a genuinely new player (one that was not
  already in that position) is rejected if that player's *own* match is not
  currently editable under the round's trigger coverage -- this is what
  stops Interchange, or any other still-open position, being used to route
  around an activated trigger's lock ("no additional player from an
  already-started club may be added", per the season model's early-lockout
  rule, now scoped to whichever club's match a *configured* trigger
  actually covers).

A permitted edit -- changing only positions not covered by any activated
trigger -- produces a normal new submitted version through the existing
#33 machinery, with the frozen positions carried through unchanged and
every earlier submitted version left untouched, exactly as issue #33
requires.

## Concurrency

`_materialize_round_triggers`, `_materialize_lineup` and the read-only
evaluation inside `guard_transition`/`lock_state` each run in their own
transaction rather than nested inside another already-open one: SQLite
allows only one writer at a time, so nesting a second write transaction
inside `submit`'s already-open one would deadlock/fail there. Sequencing
them instead (materialize the round's triggers, then the lineup's
evidence, *then* open the guarded transaction) avoids that while still
giving PostgreSQL real concurrent-writer guarantees:

- `LockoutTriggerRepository.replace` and `_materialize_round_triggers` both
  take a `FOR UPDATE` lock on the same trigger header row
  (`bbbffl_round_lockout_trigger`) before checking/recording activation, so
  a commissioner's reconfiguration and a concurrent activation observation
  cannot interleave unsafely -- one fully completes (commit or reject)
  before the other proceeds. See
  `test_trigger_replace_races_activation_and_serializes_to_one_safe_outcome`.
- `submit`'s own row lock on `weekly_lineup` similarly serializes concurrent
  submissions for one lineup; `guard_transition` runs after that lock is
  acquired and always re-reads live trigger/lock state rather than trusting
  anything the caller precomputed, so a submission prepared against a stale
  lockout-plan revision cannot succeed once that revision is no longer
  current -- it either commits against genuinely current state or fails
  with `LockedSelectionError`.
- Two independent connections racing to *observe* (not mutate) a
  never-before-evaluated lock or trigger activation rely on
  `INSERT ... ON CONFLICT DO NOTHING` to converge to one row rather than
  crashing or diverging.

Nothing here uses client/browser-supplied timestamps. See
`tests/test_lockouts_concurrency.py` (PostgreSQL-only, opt-in via
`BBBFFL_DATABASE_URL`).

## Unusual (postponed/abandoned/indeterminate) states

A trigger whose configured match(es) all have a raw status that is not one
of `afl-api`'s documented v1 values or tolerated legacy aliases (e.g. an
upstream `POSTPONED`/`ABANDONED`/`CANCELLED` value) simply does not
activate -- it is not guessed as fired or not-fired; the round's affected
players remain wherever they were (editable, or locked by a different
already-activated trigger). A *player* position is only ever `indeterminate`
in one of two cases: the round has no lockout plan configured at all
(`"lockout_plan_not_configured"`), or the player's own match cannot be
resolved (`MatchResolutionError`). Ordinary coach edits fail closed against
an indeterminate position exactly as they do against a locked one: an
unchanged resubmission is safe (nothing about it changes), but introducing
a different player is rejected.

## Read model

`LineupLockView` (`lock_state`'s return value) carries, per position, a
`PositionLockState`: `state` (`editable`/`locked`/`indeterminate`),
`reason` (`"not_yet_triggered"` / `"selective_trigger_activated"` /
`"main_lockout_triggered"` / `"lockout_plan_not_configured"` / a
`MatchResolutionError` message), `afl_match_id`, `effective_lock_at` (the
scheduled start of the player's own resolved match), `observed_status` (its
raw AFL status) and `irreversible` (true once backed by persisted
`weekly_lineup_lock` evidence). `LockoutTriggerRepository.list_triggers`
separately exposes the round's configured plan itself (trigger key, type,
sequence, associated match IDs). Together these exist so a later coach/
scorer UI can explain *why* a position is locked without recomputing any of
these rules client-side. No UI is built in this issue.

## Audit

Ordinary lock evaluation and materialization never call
`app.audit.append_event` -- see [`audit-events.md`](audit-events.md)'s
domain-neutral boundary. This mirrors `app.competition_lifecycle`'s
upstream-fact observations: recording an already-authoritative AFL/trigger
fact is not itself a privileged decision. `LockoutTriggerRepository.create`/
`.replace` **are** audited (`app.audit.LOCKOUT_TRIGGER_CONFIGURED`), since
configuring the lockout plan is itself a privileged BBBFFL competition
decision, not an observation.

## Non-goals of this issue

Carry-forward/proxy entry (#22/roadmap 22), broader ownership/position/bye
validation beyond lock integrity (roadmap 24), the coach/commissioner
selection or management UI (roadmap 25), matchup score calculation
(roadmap 26), DNP/Interchange replacement decisions (roadmap 27), scorer
result finalisation (roadmap 28), any alternate AFL schedule/status data
source, and any privileged override/correction workflow for an *already-
activated* trigger or an already-locked player selection (only
pre-activation `replace` is supported; a later authorised-correction
mechanism, if ever needed, is out of scope here). `Match.start_time_utc`
and `app.afl_client.is_recognized_match_status` are the one narrow,
documented `afl-api` client extension this issue required (both fields were
already part of the validated v1 contract; only their consumption was
missing -- see [`afl-api-v1-contract.md`](afl-api-v1-contract.md)).
