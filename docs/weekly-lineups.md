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

PostgreSQL row locks serialize the lineup, lifecycle and selected player rows,
coordinating with ownership's existing player locks. Expected draft and
submission revisions provide compare-and-swap conflict detection.

The provenance vocabulary reserves `coach`, `scorer_proxy`, `carry_forward`
and `system_derived`. This package implements the durable hooks only.
Carry-forward/proxy workflow, broader validation/warnings, UI and scoring
integration remain packages 22, 24–26. Existing 2026 Grand Final and
SuperScore JSON paths remain unchanged.

`submit`'s optional `lock_guard` parameter is package 23's (issue #34)
integration point: staged player-level AFL-match lockouts, implemented in
`app/lockouts.py`, run inside this method's own transaction and may reject
a submission that would mutate an already-locked position. See
[`lockouts.md`](lockouts.md) for the full lock rule, irreversibility and
concurrency design; this module has no lockout awareness beyond accepting
that one optional callable.
