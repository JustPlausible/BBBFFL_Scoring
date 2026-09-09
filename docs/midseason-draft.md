# Mid-season draft (issue #164)

Implementation notes for the mid-season draft workflow. See
`docs/midseason-draft-planning.md` for the agreed design/competition-process
specification this implements; this document describes what was actually
built, in the same spirit as `docs/draft-ledger.md`/`docs/preseason-trades.md`.

## Lifecycle

`app.midseason_draft.MidseasonDraftRepository` persists one `midseason_draft`
row per season, with an explicit state machine:

```
Round N final -> ladder_confirmed -> delisting_open -> delistings_locked
    -> draft_open -> draft_complete -> complete
```

- **`ladder_confirmed`** (`confirm_ladder`): requires every BBBFFL round
  through the season's configured `midseason_draft_trigger_round` to be
  `final`. Freezes an immutable copy of the calculated ladder
  (`midseason_ladder_snapshot`/`_row`/`_reference`, append-only/DB-trigger
  protected) and seeds `midseason_draft_order` from its reverse (last place
  picks first; ties broken by `season_entry_id`, matching `app.ladder`'s own
  documented non-sporting tie-break convention).
- **`delisting_open`** (`open_delisting_window`): coaches, or an audited
  Scorer proxy, submit/withdraw formal delistings (`submit_delisting`/
  `withdraw_delisting`) and propose/decide player and round-based pick
  trades (`propose_trade`/`decide_trade`).
- **`delistings_locked`** (`lock_delistings`): refuses while any trade for
  this draft is still `pending`. Releases every still-active delisted
  player's ownership (they immediately enter the available pool) and marks
  every active delisting locked.
- **`draft_open`** (`generate_selection_table` then ordinary picks):
  computes each entry's vacancy count, allocates picks via
  `vacancy_allocations`, applies any approved pick-trade legs, and hands the
  result to `app.draft.DraftRepository.materialize_draft_in_transaction`
  under `draft_kind="midseason"`. From this point selections go through the
  same `execute_pick`/`next_pick`/`correct_pick`/`finalize` engine the
  preseason draft uses.
- **`draft_complete`**: entered automatically once the final required
  selection completes and squad-size validation passes -- no separate
  Scorer lock is needed. Post-draft player trades remain available until
  `close_post_draft_trading` is called explicitly.
- **`complete`**: terminal. The season proceeds into Round 11 from the same
  `player_ownership_period` ledger every other round reads; nothing further
  is required of this module.

## Ladder snapshot + draft-order override

The live ladder (`app.ladder.LadderRepository`) is never mutated, locked, or
reordered. `confirm_ladder` takes an independent, immutable *copy* of one
`LadderSnapshot` (rows plus `result_references`) into
`midseason_ladder_snapshot`/`_row`/`_reference` at the moment the draft order
is confirmed. `override_draft_order` replaces `midseason_draft_order`'s rows
(a separate, deliberately mutable table -- no DB immutability trigger) under
an audited event with a mandatory reason; it never touches the frozen
snapshot tables. A wrong underlying result/statistic is still corrected only
through the existing audited match/result/player-stat pathways, and the
*live* ladder recalculates from that -- a later mid-season draft (a new
season, or one deliberately re-run) would then confirm a different snapshot,
but an already-confirmed snapshot is never silently rewritten.

`confirm_ladder` itself spans two transactions -- reading the season's
configured trigger round in the first, computing the ladder snapshot for it
outside any lock (deliberately: the live ladder is never locked) in
between, then freezing that snapshot into a new `midseason_draft` row in
the second. `set_midseason_draft_trigger_round` refuses only once a
`midseason_draft` row exists, so it can still change the trigger in the
window between those two transactions. The second transaction re-locks and
re-reads the season's trigger round and refuses (committing nothing) if it
no longer matches what the snapshot was computed against, rather than
freezing a ladder for a since-changed trigger round -- which the setter
being frozen once *any* draft exists would otherwise make permanent.

## Pending/approved trades and ownership

`propose_trade` validates every leg before writing anything and never
changes ownership. `decide_trade` is the only place ownership changes:
approving a **player** leg applies it immediately (release every leg, then
acquire every leg, mirroring `app.preseason.submit_trade`'s squad-capacity-
safe ordering) via `OwnershipRepository.acquire_in_transaction`/
`release_in_transaction` with `allow_closed_window=True` (the preseason
window is long closed by the time a mid-season draft runs -- see below).
Approving a **pick** leg (a round-based selection swap) is recorded but not
applied to ownership at all: there is no concrete pick to own before
generation. `generate_selection_table` applies every approved pick leg to
the computed allocation before materialising picks, so "abstract round-based
entitlements become concrete numbered picks" exactly at that boundary.
`lock_delistings` refuses outright while any trade is still `pending`
(`MidseasonPendingTradesError`, carrying every blocking `trade_id`).

A pick trade can hand a team more picks than its own vacancy count without a
matching player/pick outflow (the plan explicitly allows a temporary
imbalance) -- and, similarly, `decide_trade`'s player-leg acquisition
tolerates a temporary squad-capacity overage (`allow_capacity_overage`) when
the receiving entry holds enough still-active delistings under this same
draft to cover it once they release. Neither of these is left to be
discovered only once a pick can never legally execute: `lock_delistings`
re-plans the eventual selection allocation before ever committing (shared
with `generate_selection_table` via `_plan_selection_allocations`) and
refuses to lock -- rolling back cleanly, including any delisted-player
releases already applied -- if any entry's final selection count doesn't
exactly match its own vacancies, if any approved pick leg has no vacancy
to apply to at all, or if any entry's *live* squad size is still over the
configured limit (`MidseasonPickReconciliationError`, carrying
`.mismatched`, `.unapplied_leg_ids` and `.overfull`) -- the last of these
catches a delisting that covered a capacity-overage acquisition at
approval time being withdrawn again before lock, which `max(squad_limit -
count, 0)` alone would otherwise silently clamp away. The capacity-overage
bypass is itself bounded the same way: outside `delisting_open`, or
without a covering active delisting, `decide_trade` refuses the
acquisition outright instead of creating an overage nothing will ever
resolve. Redirects are
resolved as a single direct hop from each allocation's own original
vacancy-owner -- never chained through a leg's destination -- since a leg
only ever names its sender's own original entitlement, not a specifically
re-traded one; this also makes same-round swaps and multi-way rotations
resolve correctly without special-casing.

`decide_trade` only ever accepts a *pending* trade, so an already-*approved*
one has no way back through it. `reverse_trade_approval` is the exceptional
Scorer correction for exactly that: while the delisting window is still
open, it undoes a player leg (release from the current holder, reacquire
back to the original owner) and simply drops a pick leg's `approved` status
so `_plan_selection_allocations` stops considering it -- the real recovery
path for a pick leg `lock_delistings` finds undeliverable (e.g. a round
number no entry's vacancy could ever reach).

If literally no entry has any vacancy at all -- nobody delisted anyone this
cycle, or every delisting was withdrawn before lock -- `generate_selection_
table` recognises that as a trivial completion (straight to `draft_complete`,
no engine draft ever created) rather than raising: `app.draft.
DraftRepository` refuses to materialise a zero-pick draft outright, and
`delistings_locked` has no route back to `delisting_open` to retry from.
Every downstream read (`status`, `picks`, `next_pick`,
`reconcile_completion`) is already null-safe for "no engine draft exists".

## Delisting lock and draft-generation boundary

`lock_delistings` and `generate_selection_table` are deliberately two
separate, explicit actions (rather than one combined step) so the lock
boundary and the generation boundary each get their own audited event and
state transition, matching the plan's own two numbered steps. Generation is
only reachable from `delistings_locked`.

## Draft engine reuse (`app.draft`)

`app.draft.DraftRepository`'s `season_draft` table gained a `draft_kind`
discriminator (`'preseason'` default, `'midseason'` new; unique per
`(season_id, draft_kind)` rather than per season) so one season can carry
both an original preseason draft and a later mid-season draft, fully
independently. Every repository method that resolves "the draft for this
season" (`status`, `order`, `picks`, `corrections`, `pause`, `resume`,
`execute_pick`, `correct_pick`, `finalize`, `reopen`, `next_pick`) takes an
optional `draft_kind` (defaulting to `"preseason"`, so every existing
caller is unaffected). A new `materialize_draft_in_transaction` factors out
`accept_order`'s "freeze a team order and insert every pick" mechanics so a
caller with its own preceding writes in the same transaction (mid-season's
delisting releases and vacancy computation) gets one atomic commit, and so
the allocation itself is pluggable: `snake_allocations` (uniform picks per
team) for preseason, `app.midseason_draft.vacancy_allocations` (picks vary
by vacancy count, skipping a satisfied team) for mid-season.

Because the preseason trading window is always closed by the time a
mid-season draft runs, every mid-season ownership mutation (`execute_pick`,
`correct_pick`, and every write in `app.midseason_draft` itself) passes
`allow_closed_window=True` to `OwnershipRepository`'s `_in_transaction`
methods -- the same narrow escape hatch `app.preseason.
correct_opening_snapshot` already uses, never exposed on the ordinary
public `acquire`/`release`/`transfer` entry points.

## Replay-specific proxy behaviour

For the 2026 replay (and any operator-driven use before coach-facing
workflows exist), every mutating call takes an explicit `actor:
ActorContext`. Delistings, trades and picks submitted by a Scorer/Admin/
Replay Operator on a team's behalf are recorded as `anonymous_operator`
with the operator's own role (`scorer`/`admin`/`replay_operator`), exactly
matching the existing draft/preseason proxy convention -- there is no
separate "proxy" actor type or marker column; provenance is the audit
event's `actor_type`/`actor_role`/`reason`. `scripts/replay_2026_midseason_draft.py`
exposes one subcommand per real repository action (rather than fabricating a
whole scenario) so an operator can apply each actual historical decision as
evidence becomes available; its one deliberately synthetic convenience,
`auto-complete`, fills remaining picks from the available pool only once no
further evidence exists, always logged as such.

## Deferred (2027 coach-facing convenience)

Not implemented, per the issue's explicit scope:

- private coach list-planning (Keep/Potential delist/Potential trade tags);
- a dedicated Scorer/coach HTML dashboard page for this workflow (the JSON
  API in `app/routes/midseason_draft.py` is complete and testable; a page
  can be added on top of it later without further domain changes, the same
  layering `app/templates/draft.html`/`preseason.html` already use);
  in-app trade proposal confirmation by both coaches before Scorer/Admin
  approval;
- timed auto-pick from a private preference list;
- automatic closure of post-draft trading tied to Round 11's own lockout
  trigger (`close_post_draft_trading` is an explicit Scorer action, matching
  `app.preseason.close_window`'s "the Scorer decides when the phase is
  closed" convention).

## Schema

See `migrations/versions/0027_midseason_draft.py`: `bbbffl_season` gains a
nullable `midseason_draft_trigger_round` column (`app.season.SeasonRepository
.set_midseason_draft_trigger_round`, frozen once a mid-season draft exists);
`season_draft` gains `draft_kind`; new tables `midseason_draft`,
`midseason_ladder_snapshot`/`_row`/`_reference` (immutable), `midseason_draft_order`,
`midseason_delisting`, `midseason_trade`/`_leg`.
