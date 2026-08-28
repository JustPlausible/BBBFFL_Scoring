# Preseason draft ledger

The season draft is authoritative for accepted order, stable pick identity,
effective pick owner, transfers, and immutable completed-pick history. It is not
a roster: `player_ownership_period` remains the only authority for player ownership.

Acceptance requires every season entry exactly once and atomically materialises
all picks using the configured squad limit. Odd rounds traverse the accepted
order and even rounds reverse it, naturally allowing consecutive turnaround
picks. Each pick has a durable UUID, contiguous overall number, round, immutable
original allocation, and independently transferable effective owner. Transfers
do not move picks and append both transfer provenance and an audit event.
Once accepted, ordinary squad-limit configuration cannot change that snapshotted
target, keeping materialised pick count and authoritative ownership capacity in
lockstep.

Selection locks and identifies the lowest uncompleted pick without client turn
state. The command validates its owner and delegates acquisition to the existing
ownership repository on the same transaction, including player eligibility,
availability, season identity, and squad capacity checks. Only then are the pick
and correlated audit events completed. PostgreSQL row locks and ownership-player
locks prevent competing completion/acquisition; SQLite obtains its writer lock
before validation for development and tests. Any rejection rolls everything back.

Completed picks retain durable season-entry and season-player IDs, independent
of mutable names or subsequent roster ownership. Database triggers reject
ordinary completed-result rewrites and deletion.

Roadmap package 14 (issue #53) builds the scorer-operated draft workflow on
top of this ledger: persisted pause/resume, an auditable correction/undo
mechanism for the most recently completed pick, and guarded finalisation,
all as an operator surface over this same repository. See
[`scorer-draft-workflow.md`](scorer-draft-workflow.md) for that workflow and
migration `0016_draft_operations` for the small additions it required
(`season_draft.paused_at`/`finalized_at`, `draft_pick.superseded_by_draft_pick_id`,
and the append-only `draft_pick_correction` table) — the correction
mechanism never rewrites or deletes a completed pick's row; it points it at
a fresh replacement row for the same slot instead.
