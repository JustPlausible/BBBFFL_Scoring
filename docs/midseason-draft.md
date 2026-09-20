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

## Web UI and coach self-service (issue #181)

The items this section previously deferred to "2027" are now implemented,
on top of the same domain layer above with no changes to its rules:

- **Shared draft board.** `app/draft_board.py`'s `build_board`/
  `build_readiness`/`player_browse_view` are parameterised by `draft_kind`
  and used by both `app/routes/draft.py` (preseason) and
  `app/routes/midseason_draft.py` (mid-season, once a pick table exists via
  `generate_selection_table`) -- one shared player-discovery/selection
  experience, not two independent pickers. `app/templates/draft.html` is
  itself `draft_kind`-aware (`{{ draft_kind }}`/`{{ api_base }}`), reused
  verbatim at `/admin/midseason-draft/{season_id}/conduct`.
- **Coach self-service selection.** `midseason_draft.participate`
  (granted to Coach, alongside the existing `midseason_draft.manage`
  Scorer/Admin/Replay-Operator authority) lets a coach whose team owns the
  active pick submit it directly through `POST /api/admin/midseason-draft/
  {season_id}/pick` -- gated by `require_entry_context`, the same rule the
  preseason board already used. A proxy pick on behalf of a different team
  still requires the full `midseason_draft.manage` authority. Every
  existing turn/ownership/capacity/concurrency check in
  `MidseasonDraftRepository.execute_pick`/`app.draft.DraftRepository.
  execute_pick` is unchanged and still authoritative.
- **Private coach shortlist.** `app/shortlist.py`'s `ShortlistRepository`
  (`coach_draft_shortlist` table, migration 0035) is a season-entry-scoped
  ordered preference list, usable before/during either draft type. It never
  reserves a player or changes ownership; `suggestion()` recomputes the
  highest-ranked still-available preference fresh on every read. Privacy is
  enforced entirely by `app/routes/shortlist.py`'s use of
  `require_entry_context` -- the module itself trusts its caller.
- **Mid-season operations page.** `/admin/midseason-draft/{season_id}`
  (`app/templates/midseason_draft_operations.html`) walks the lifecycle
  above with human-readable team/coach/player labels (never a bare id as
  the primary display), a ladder/reverse-order preview
  (`GET .../ladder-preview`) before `confirm-ladder` becomes irreversible,
  and delisting/lock/generate controls. `app/admin_dashboard.py`'s
  `midseason_draft_dashboard_status` surfaces an "obvious action card" once
  the configured trigger round is fully final and no draft exists yet.
- `scripts/replay_2026_midseason_draft.py` now resolves team/coach/player
  names in its printed output, retaining the raw id in parentheses.

Still deferred, per the issue's explicit non-goals: in-app trade proposal
confirmation by both coaches before Scorer/Admin approval; timed auto-pick
from a private shortlist; automatic closure of post-draft trading tied to
Round 11's own lockout trigger (`close_post_draft_trading` remains an
explicit Scorer action).

## Navigation, competition resolution and Coach delisting UX (issue #226)

Acceptance testing of the issue #181 UI against a restored 2026 replay
checkpoint found three practical gaps in the same "no CLI/UUID/database
knowledge" journey; each is a navigation/presentation fix over the
unchanged domain layer above, not a new capability:

- **Persistent mid-season setup navigation.** Before a trigger round is
  configured, `midseason_draft_dashboard_status` correctly returns `None`,
  so the conditional Admin Dashboard readiness card has nothing to show.
  Season Centre's `links.midseason_draft`
  (`app/season_centre.py`'s `_links`) is a *persistent* entry to
  `/admin/midseason-draft/{season_id}` instead, gated only on the season
  having regular-season rounds created -- present before, during and after
  trigger configuration, for Secretary/Admin. The Scorer Operations
  Dashboard carries the same persistent link (in its static "Which workflow
  do I need?" panel) for Scorer/Replay-Operator/Admin. The conditional
  Admin Dashboard readiness card is unchanged and still appears once the
  trigger round is fully final -- the stronger operational cue at that
  point, per the issue.
- **Ordinary competition resolution.** `GET .../ordinary-competitions`
  returns every `stream_type='ordinary'` competition for the season with
  its human-readable label. `midseason_draft_operations.html` auto-selects
  when there is exactly one (the normal case), offers a `<select>` when
  there is more than one, and disables ladder preview with an explanatory
  message when there is none -- the operator never types or pastes a raw
  competition UUID. `ladder-preview`/`confirm-ladder` still perform their
  own `stream_type='ordinary'` + season-ownership validation exactly as
  before; this is presentation only.
- **Coach self-service delisting.** `app/routes/coach_delisting.py` is a
  thin Coach-facing surface over the exact same
  `MidseasonDraftRepository.submit_delisting`/`withdraw_delisting` calls
  the Scorer/Admin proxy path already used -- no new domain model, no
  duplicated validation. `MidseasonDraftRepository.coach_delisting_context`
  finds the season entry (if any) this coach owns in whichever season is
  currently `delisting_open`; the Coach Account page (`/account`) shows an
  unmistakable cue and link into `/account/delisting` only while that is
  true. Authorization is `require_entry_context` -- the same "coach owns
  it, or a delegated role is representing it" primitive
  `app/routes/shortlist.py`/`submit_pick` already use -- so a
  client-supplied `season_entry_id` is always checked against the
  authenticated principal, never trusted on its own; withdrawal
  additionally checks the named delisting's own `season_entry_id` against
  that same resolved entry. The Scorer/Admin proxy controls on
  `/admin/midseason-draft/{season_id}` are unchanged and remain the
  exceptional/audited path.

## Schema

See `migrations/versions/0027_midseason_draft.py`: `bbbffl_season` gains a
nullable `midseason_draft_trigger_round` column (`app.season.SeasonRepository
.set_midseason_draft_trigger_round`, frozen once a mid-season draft exists);
`season_draft` gains `draft_kind`; new tables `midseason_draft`,
`midseason_ladder_snapshot`/`_row`/`_reference` (immutable), `midseason_draft_order`,
`midseason_delisting`, `midseason_trade`/`_leg`.
