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

Submission accepts only an `open` persisted BBBFFL round, stable season-player
IDs currently owned by the entry, and no duplicate selected player. Ownership
is queried from the existing ledger and is **not copied as a second current-owner
authority**. Submitted player IDs remain intact after a later release or trade.

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
