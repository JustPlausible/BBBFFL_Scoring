# Weekly lineup persistence

Weekly selections are scoped by stable season, competition stream, persisted
BBBFFL round and season-entry IDs. The nine scoring positions have the stable
identities `F1`, `F2`, `F3`, `M1`, `M2`, `M3`, `Ruck`, `Tackler` and
`Interchange`; mutable names and an afl-api “current round” are never keys.

## Authority and visibility

`weekly_lineup` and `weekly_lineup_draft_slot` are private mutable working
state. A draft starts empty (all nine positions are persisted with null player
IDs), and every save compare-and-swaps an explicit `draft_revision`.

`weekly_lineup_submission` and `weekly_lineup_submission_slot` are the official
historical selection authority. Submission copies all nine draft positions into
a new immutable version, records actor/source/time provenance, and advances the
aggregate's effective-version pointer transactionally. Database triggers reject
updates and deletes of snapshots. Replay and scoring consumers must read a
specific submitted version (or the effective submitted version); neither a
mutable draft nor prototype JSON is a substitute.

Submission accepts an `open` or `live` persisted BBBFFL round (issue #144;
see "`open` vs. `live`: two independent dimensions" below), stable
season-player IDs currently owned by the entry, and no duplicate selected
player. Ownership is queried from the existing ledger and is **not copied as
a second current-owner authority**. Submitted player IDs remain intact after
a later release or trade.

A formal submission is not required to name a player in every position
(issue #98). One or more positions may be deliberately left `null` -- a
vacant position is legitimate, authoritative competition state, immutably
recorded exactly like any populated one, and is never rejected, never
silently filled, and never confused with a scorer's later DNP ruling on a
named player or with missing/corrupt input (see `docs/lineup-validation.md`
for the validation-layer distinction). Duplicate-player and ownership
validation still apply to every populated position. A vacant position
remains an eligible Interchange target under the existing scoring rules
(`docs/lockouts.md`, `app.round_review`/`app.service`), and stays visible on
scorer/public submitted-lineup views as an intentional vacancy rather than
as unavailable or corrupt data.

The coach-facing `Submit Lineup` web flow (`app/routes/coach_lineup.py`)
adds one UX-only safeguard on top of this: if the draft about to be
submitted leaves one or more of the eight ordinary (non-Interchange)
positions vacant, submitting first shows a confirmation step naming those
positions and explaining the Interchange consequence, rather than silently
creating the submitted version. Declining (navigating away without posting
the confirmation) creates nothing; confirming submits the same content
exactly as already saved. This is purely a "did you mean to leave this
empty" prompt for the coach -- it carries no domain meaning, is invisible
to `app.lineup_validation`/`app.lineups`, and never turns into a required
field, a fabricated selection, or an API-level requirement. A vacant
Interchange alone never triggers it.

PostgreSQL row locks serialize the lineup, lifecycle and selected player rows,
coordinating with ownership's existing player locks. Expected draft and
submission revisions provide compare-and-swap conflict detection.

The provenance vocabulary reserves `coach`, `scorer_proxy`, `carry_forward`
and `system_derived`. This package implements the durable hooks only.
Carry-forward/proxy workflow, broader validation/warnings, UI and scoring
integration remain packages 22, 24–26. Existing 2026 Grand Final and
SuperScore JSON paths remain unchanged.

`submit`'s optional `lock_guard` parameter is package 23's (issue #34)
integration point: staged player-level AFL-match lockouts driven by a
persisted, commissioner/scorer-configured round lockout plan (`app/lockouts.py`).
When `lock_guard` exposes a `.materialize(lineup_id)` method, `submit` calls
it *before* opening its own transaction; the guard itself then runs inside
this method's own transaction and may reject a submission that would
mutate an already-locked position. See [`lockouts.md`](lockouts.md) for the
full lock rule, irreversibility and concurrency design; this module has no
lockout awareness beyond accepting
that one optional callable.

The same `lock_guard` parameter is also how an Opening Round deferred
nomination's locked slot is enforced
(`app.opening_round.OpeningRoundSelectionGuard`, issue #69) -- rejecting
any submission, whatever its source, that would place a different player
into a slot a nomination already owns. It composes with an ordinary
`app.lockouts.LockGuard` (via its `inner` argument) rather than replacing
it, so both mechanisms govern the same lineup without either weakening the
other. See [`opening-round-deferred-selection.md`](opening-round-deferred-selection.md).

## `open` vs. `live`: two independent dimensions (issue #144)

A BBBFFL round's persisted lifecycle
([`competition-lifecycle.md`](competition-lifecycle.md)) and its
position-level lock state ([`lockouts.md`](lockouts.md)) answer two
different questions, and conflating them was issue #144's defect:

- **Lifecycle** (`open` -> `live` -> `review` -> `final`) answers "is
  ordinary lineup submission in scope for this round at all?" `live` means
  the round's first AFL match has started -- nothing more.
- **Position-level lock state** (`app.lockouts`) answers "is *this
  particular* position, right now, legal to change?" It is governed
  entirely by the round's configured selective/main trigger plan, never by
  the round's own lifecycle field.

`_finalize_submission` (the one core `submit`/`submit_positions` share)
therefore accepts an ordinary submission attempt for a round in either
`open` or `live`, and -- unchanged from before this issue -- always
delegates the actual per-position decision to `lock_guard`. A round
becoming `live` at its first AFL match does **not** itself freeze
submission: staged lockout means most positions are typically still
individually editable for a while afterwards, and the round should reflect
that accurately rather than staying artificially `open` until the main
lockout. `review` and `final` remain outside the states an ordinary
submission accepts at all, regardless of `lock_guard`.

Because `lock_guard` is the *only* thing standing between a `live` round
and an unrestricted rewrite of every position, `_finalize_submission`
fails closed if a round is `live` and no `lock_guard` was supplied at
all -- every production submission source (`app.coach_lineup`,
`app.lineup_proxy`, `app.carry_forward`) always supplies a real one built
from `app.lockouts.LockoutRepository.guard`, composed with
`OpeningRoundSelectionGuard` where a deferred nomination also applies; this
is a defensive invariant for any future submission source, not a change to
those paths. This rule applies uniformly to the coach's own submission,
scorer/admin/replay-operator proxy submission, and carry-forward -- there is
one authoritative rule (`app.lineups.ORDINARY_SUBMISSION_ALLOWED_STATES`),
never a per-route re-implementation of the lifecycle gate.

## Authorised correction of an already-locked lineup (issue #137)

Ordinary submission (`submit`/`submit_positions`, whichever `source_type`)
always goes through `lock_guard` and always requires the round to be
`open` or `live` (issue #144) -- neither of those loosens for a coach, a
scorer/admin proxy entry, carry-forward, or any future ordinary source.
That is deliberate: none of them may ever place or move a player into an
already-locked position, no matter who is acting or which lifecycle state
the round is in.

Real competitions still occasionally need exactly that: a coach names a
player in the wrong position in a league-chat message, or a scorer
transposes a position while entering a delegated lineup, and the error is
only noticed after the covering AFL match has already started. Historically
the league discusses the case and the scorer either makes or refuses the
adjustment by hand; the system must preserve that decision and its reason
rather than force a raw database edit, a checkpoint rollback, or a
score-only override -- none of which would preserve authoritative lineup
history.

`WeeklyLineupRepository.submit_correction` is that one narrow door,
distinct from every ordinary submission path:

- `source_type="scorer_correction"` -- its own value in
  `SUBMISSION_SOURCES`, distinct from `"scorer_proxy"`: an ordinary proxy
  submission still goes through `lock_guard` exactly like a coach's own
  submission (see `app.lineup_proxy`) and gets no special exemption.
- Permitted for any round state in `CORRECTION_ALLOWED_STATES` (`"open"`,
  `"live"`, `"review"`) rather than only `"open"` -- the whole point is
  correcting a lineup *after* a lockout has activated, which by definition
  means the round has moved past `open`. A round that has already reached
  `"final"` publication raises `RoundPublishedError` instead: this
  workflow never edits published official history, which remains
  `app.round_review.attempt_correction`'s separate boundary (see
  [`scorer-round-review.md`](scorer-round-review.md)).
- Never invokes `lock_guard` -- an authorised correction is exactly the one
  path permitted to override an already-locked position. It still accepts
  only the ordinary, atomic, whole-lineup validation every other submission
  source gets (`_normalise`'s no-duplicate-player/legal-position rules,
  `_validate_players`/`_validate_ownership`'s season/ownership checks): a
  correction is authorised to bypass the *lock*, never ordinary lineup
  integrity.
- Rejects any change to a position with an active Opening Round deferred
  nomination (`opening_round_nomination`) -- that slot's own separate
  audited correction workflow
  (`app.opening_round.OpeningRoundNominationRepository.correct`, see
  [`opening-round-deferred-selection.md`](opening-round-deferred-selection.md))
  remains the only door into it.
- Requires a substantive reason and at least one actually-changed position
  (`NoOpCorrectionError` otherwise) -- a correction records a deliberate
  competition decision, never a content-free resubmission.

Like every other submission source, a correction creates a brand-new
immutable `weekly_lineup_submission` version (`to_version =
from_version + 1`) via the *same* `weekly_lineup.effective_submission_
version` compare-and-swap and the *same* database-trigger-enforced
immutability every prior version already has -- it never edits
`from_version`'s row, and `weekly_lineup_lock`'s existing evidence for the
positions involved is never read for write purposes, only copied read-only
into the correction's own provenance record. Two new immutable,
trigger-protected tables (`migrations/versions/0025_lineup_correction.py`)
carry that provenance:

- `weekly_lineup_correction` -- one row per correction: `from_version`/
  `to_version` (always sequential), the round/entry, actor/role, the
  required reason, and the timestamp.
- `weekly_lineup_correction_slot` -- one row per position the correction
  actually changed: the previous and corrected player, and, copied
  verbatim from `weekly_lineup_lock` at correction time if that position
  was locked, which trigger/AFL match/instant locked it. This is what lets
  a corrected occupant of a formerly-locked slot retain defensible
  provenance tracing back to the already-active trigger, without
  fabricating a new, later lock event or touching `weekly_lineup_lock`
  itself -- the original lock evidence, and every prior submitted version,
  remain byte-for-byte intact and independently readable
  (`get_submission`/`get_correction`/`list_corrections`).

An audit event (`app.audit.LINEUP_CORRECTED`) records the same before/after
positions, actor, role and reason, sharing one `correlation_id` with the
correction's own `LINEUP_SUBMITTED` event so both read back as one logical
command.

### Authority

`app.lineup_correction.LineupCorrectionService` is the reason-checked,
human-readable entry point most callers use: it merges a caller's partial
`{position: season_player_id}` change map onto the lineup's current
effective positions (so a Tackler <-> Interchange swap only needs to name
those two slots) and rejects any actor that is not an `anonymous_operator`
with `actor_role` in `{"scorer", "admin", "replay_operator"}` --
`UnauthorizedCorrectionActorError` otherwise. This mirrors, but is
independent of, generic proxy-entry authority (`app.lineup_proxy`):
broad `lineup.proxy` capability alone is never treated as sufficient here.

`app/routes/lineup_correction.py` (`/api/admin/lineup-correction`) gates
every request behind the season-scoped `lineup.correct_locked` capability
(`app.authorization.CAPABILITIES`) -- granted to Scorer and Administrator
unconditionally, and to Replay Operator only for a season that role has
actually been granted for (`require_role_covers_season`, exactly as
`app.round_review`/Opening Round operations already require -- see
[`acting-context.md`](acting-context.md)). Ordinary Coach authority never
carries this capability. The browser page (`/scorer/lineup-correction`)
shows the current effective lineup with human-readable player/club/lock
evidence, an atomic corrected-position editor, a required reason field, a
before/after preview, an explicit warning and confirmation that this
overrides an activated lock as an authorised competition decision, and the
complete correction history -- reloading every read surface from the
corrected authoritative state once applied.

### Calculation and review interaction

A correction never emulates itself as a numeric score override. Because
`app.calculations.MatchupCalculationService` always reads a lineup's
*current* `effective_submission_version`, a calculation run after a
correction automatically scores the corrected lineup with no correction-
specific code of its own. `app.round_review.build_matchup_review` compares
each side's *already-calculated* snapshot's `lineup_version` against the
lineup's current effective version and adds a blocker
("...lineup was corrected...; recalculate before sign-off") whenever they
differ, so a correction made after calculation but before sign-off is
never silently reviewed against stale evidence -- recalculation (which
`/signoff` already always does immediately before validating readiness)
clears it. After the round reaches `"final"` publication,
`submit_correction` refuses outright (`RoundPublishedError`); the operator
is directed to `app.round_review.attempt_correction`'s separate
official-result correction workflow instead.

## Audited adjudication of a missed initial submission after lockout (issue #146)

Issue #137's correction above fixes an *existing* authoritative
submission. It has nothing to offer a different, narrower failure: a coach
saves a private draft before a lockout, believes it is submitted, and
never actually creates an authoritative submission before an activated
trigger closes ordinary submission for one or more positions. Ordinary
submission then correctly refuses the draft's now-locked players
(`LockedSelectionError`, issue #144), and `submit_correction` correctly has
no prior submission to correct (`NoEffectiveSubmissionError`) -- there is
no in-app path back to a submitted lineup at all.

**This is a league decision, made outside the application, never an
in-app vote or approval flow.** `app.lineup_adjudication` implements no
voting, quorum membership, ballot counting or approval collection of any
kind. The authorised Scorer/Administrator simply records, as a substantive
reason, the outcome the league already reached through its normal
consultation process -- in exactly the same style `submit_correction`
already requires for its own reason -- and selects one of two outcomes:

- **`accept_evidenced_draft`** -- the league approved capturing the
  coach's saved pre-lockout draft as the round's first authoritative
  submission.
- **`apply_carry_forward`** -- the league rejected the late request (or
  the draft's evidence was insufficient), so the round's first
  authoritative submission is instead sourced from the previous round's
  effective submitted lineup, under the established BBBFFL carry-forward
  rules.

Both create the lineup's *first* submission (`version` always `1`) through
`WeeklyLineupRepository.submit_adjudicated_first_submission`, permitted
only while the round is `live` or `review` (`ADJUDICATION_ALLOWED_STATES`
-- narrower than correction's own frozenset: `open` is excluded too, since
no lock could possibly have activated yet). It refuses outright
(`EffectiveSubmissionExistsError`) if an effective submission already
exists -- checked under the same `weekly_lineup` row lock/CAS every other
submission path uses, so this is a real, concurrency-safe guarantee, not
merely a UI precondition.

### Evidence: what a private draft can actually prove

A saved draft is default evidence only of what the *current* draft
revision holds and when the *whole draft* was last saved
(`weekly_lineup.updated_at`) -- and that single, whole-draft timestamp is
not reliable evidence once a coach legitimately edits a still-unlocked
position *after* a selective lockout has already activated: the timestamp
advances even though the already-locked positions were never touched.

Rather than introduce general-purpose draft history/versioning, issue #146
adds the minimum additional evidence needed: `weekly_lineup_draft_slot`
(migrations/versions/0026_lineup_adjudication.py) now carries its own
`updated_at`/`actor_type`/`actor_id`/`actor_role` columns *per position*,
advanced by `save_draft` only when that position's value actually changes.
An untouched position keeps whatever timestamp/actor it already had,
tracing back to when it was first set -- so a later, legitimate edit to a
different, still-open position can never contaminate the evidence already
established for an unrelated, already-locked one.

`app.lineup_adjudication.LineupAdjudicationService._resolve_evidenced_positions`
is the one place that turns this into a per-position decision, using the
same `app.lockouts.LockoutRepository` trigger-coverage/lock evaluation
every ordinary submission uses (three small, explicitly-named public
wrappers -- `materialize_round_triggers`/`trigger_coverage_locked`/
`evaluate_draft_position_locked` -- expose exactly what this needs without
duplicating any lock-decision logic, and without `app.lineups` importing
`app.lockouts`, which the existing module layering disallows):

- a position an active Opening Round deferred nomination governs always
  resolves to the nominated player (issue #69's existing rule, never
  reinterpreted);
- an empty draft position is simply vacant -- there is nothing to prove
  wrong about an empty slot;
- a position that is not currently locked resolves to whatever the
  current draft holds, and remains completable through the ordinary
  `live` submission workflow after this capture;
- a locked (or indeterminate) position resolves to the draft's current
  value only if that position's own `updated_at` is at or before the
  covering trigger's durable `effective_lock_at` (`evidence_status=
  "proven_pre_lock"`); otherwise it resolves to vacant
  (`"unproven_defaulted_vacant"`). The operator has no way to substitute,
  move or add a locked player beyond what this evidence proves --
  `accept_evidenced_draft` accepts no position overrides at all.

This evaluation runs twice: once outside any transaction for the
before-confirmation preview (`describe_candidate`), and again *inside*
`submit_adjudicated_first_submission`'s own transaction, reading the draft
and trigger coverage fresh on that transaction's connection -- so a
trigger that activates, or a draft edit that lands, between the preview
and the confirmed decision is always caught by the authoritative second
evaluation.

### Carry-forward fallback

`apply_carry_forward_fallback` sources the current round's first
submission from `app.carry_forward.CarryForwardService.resolve_source` --
the same previous-round resolution ordinary carry-forward uses -- and
re-validates the source is unchanged *inside* the same transaction,
exactly like ordinary carry-forward's own `require_unchanged`. It never
reads or merges anything from the entry's own rejected private draft: the
only positions added on top of the copied source are the current round's
own active Opening Round deferred nominations, resolved the same way as
Resolution A. Its own distinct `source_type`
(`"scorer_adjudicated_carry_forward"`, alongside `"scorer_late_capture"`
for the evidenced-draft path) keeps both permanently distinguishable from
an ordinary, pre-lockout `"carry_forward"` submission.

### Audit/provenance

`lineup_adjudication`/`lineup_adjudication_slot`
(migrations/versions/0026_lineup_adjudication.py) are structurally
parallel to issue #137's `weekly_lineup_correction`/
`weekly_lineup_correction_slot`: an immutable header (decision type, the
resulting version -- always `1` -- actor/role, the required substantive
reason, and either the source draft revision/timestamp or the previous
round/version this fallback carried forward) plus one row per position
recording its resolved value and evidence status, and, for a position that
was locked, the trigger/match/effective-lock evidence it traces back to.
An audit event (`app.audit.LINEUP_ADJUDICATED`) shares one `correlation_id`
with the submission's own `LINEUP_SUBMITTED` event, exactly like
`LINEUP_CORRECTED` already does for issue #137.

### Authority

`app.lineup_adjudication._ensure_adjudication_actor` rejects any actor
that is not an `anonymous_operator` with `actor_role` in `{"scorer",
"admin", "replay_operator"}` (`UnauthorizedAdjudicationActorError`
otherwise) -- Coach and ordinary delegated/proxy authority
(`app.lineup_proxy`) are never sufficient. `app/routes/lineup_adjudication.py`
(`/api/admin/lineup-adjudication`) gates every request behind its own,
dedicated season-scoped `lineup.adjudicate_missed_submission` capability
(`app.authorization.CAPABILITIES`) -- distinct from both `lineup.proxy`
and `lineup.correct_locked` -- granted to Scorer and Administrator
unconditionally, and to Replay Operator only for a season that role has
actually been granted for (`require_role_covers_season`). The browser page
(`/scorer/lineup-adjudication`) shows the activated trigger evidence, the
evidenced-draft preview (per-position resolved value, evidence strength,
and trigger/lock detail) or the carry-forward preview (source round/
version/positions), a required reason field recording the external
league decision, and an explicit confirmation that the operator is
creating the team's *first* authoritative submission after lockout --
never implying the application itself approved the request or conducted
any consultation.
