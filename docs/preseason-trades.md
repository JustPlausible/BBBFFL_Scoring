# Preseason trade/finalisation window and the frozen opening squad

**Issue:** [#54 — Finalise opening squads and preseason trades](https://github.com/JustPlausible/BBBFFL_Scoring/issues/54)
(roadmap work package **15**, `docs/roadmap/2027-season-roadmap.md`)

**Depends on:** the player ownership ledger (roadmap package 11, issue #21,
[`player-pool-ownership.md`](player-pool-ownership.md)) and the finalised
preseason draft (roadmap package 14, issue #53,
[`draft-ledger.md`](draft-ledger.md), [`scorer-draft-workflow.md`](scorer-draft-workflow.md)).

## What this is

The final preseason step between a finalised draft and Round 1: a season-
scoped, persisted **preseason transaction/finalisation window** during which
scorer/admin-approved player trades happen, followed by an explicit,
validated **close** that freezes an authoritative opening-ownership
snapshot. It is deliberately an operator surface over
`app.preseason.PreseasonRepository`, which itself builds directly on
`app.player_pool.OwnershipRepository` -- every ownership invariant (no
overlapping ownership, season match, eligibility, squad capacity) is
enforced exactly once, by the same ledger a draft pick or an ordinary
transfer already uses.

## Lifecycle

```
preseason draft -> draft finalised -> preseason trade/finalisation window (open)
    -> [preseason trades] -> window closed (opening squads frozen) -> Round 1
```

`season_preseason_window` holds exactly one row per season (like
`season_draft`): `closed_at IS NULL` means open. State is persisted BBBFFL
domain state, not inferred from dates, an in-memory flag, or whether draft
picks happen to exist -- and it is season-scoped, so a 2026 replay window
and a live 2027 season's window can never observe or affect each other even
sharing one database (`tests/test_preseason.py::test_one_seasons_window_does_not_affect_another`).

- `PreseasonRepository.open_window(season_id)` requires that season's draft
  to already be finalized (`DraftStatus.is_finalized`) -- an unfinalized or
  entirely absent draft raises `PreseasonDraftNotFinalizedError`. A season
  can only ever open one window (`PreseasonWindowExistsError` on a second
  attempt).
- `PreseasonRepository.close_window(season_id)` is covered in "Closing the
  window" below.

## Submitting a trade

`POST`-equivalent: `PreseasonRepository.submit_trade(season_id, legs, ...)`,
exposed at `POST /api/admin/preseason/{season_id}/trade`. `legs` is a list
of `{season_player_id, from_season_entry_id, to_season_entry_id}` mappings
-- the caller's claimed current owner is always validated against the
authoritative ledger, never trusted. This supports, with no special-cased
code per shape:

- an ordinary two-club trade (two legs, opposite direction);
- a multi-club trade (three or more entries in one trade, e.g. a rotation:
  A gives to B, B gives to C, C gives to A);
- several players moving in one trade, including several players moving
  between the same two entries.

A trade is one atomic transaction, not a sequence of independent ownership
edits:

1. every leg is validated first -- required fields, no duplicate player
   within the trade, the player belongs to this season's pool and is
   eligible, both entries belong to this season, and (checked against the
   ledger, not the caller's claim) the player is currently owned by the
   claimed `from_season_entry_id`. Any failure raises
   `PreseasonTradeValidationError` with `.issues` (one entry per problem,
   identifying the offending leg) **before any write happens**;
2. only once every leg passes does it apply every release, then every
   acquisition, through `OwnershipRepository.release_in_transaction`/
   `acquire_in_transaction` -- releases first, so a multi-player trade's
   squad-capacity check sees each entry's net position for the whole trade,
   not a stale mid-trade count;
3. the whole call runs inside one database transaction
   (`app.db.transaction`), so a failure discovered only during step 2 (e.g.
   `SquadCapacityError`, or a concurrent `PlayerUnavailableError`) rolls
   back everything written so far in that same trade -- no partial trade is
   ever visible, and no audit event describes a trade that didn't fully
   apply (see `tests/test_preseason.py`'s atomic-rollback tests).

`preseason_trade` records one row per applied trade (season, window, actor's
audit event, correlation ID, reason, timestamp); `preseason_trade_leg`
records one append-only row per player moved, linking directly to the exact
`player_ownership_period` rows it released and acquired -- full provenance
from a trade back into the ledger. Both tables reject ordinary
`UPDATE`/`DELETE` at the database level (migration `0017_preseason`), the
same immutability pattern roadmap 13/14's draft tables already use.

## Squad validation

`PreseasonRepository.validate_squads(season_id)` compares every season
entry's live active-ownership count against the season's configured
`season_squad_configuration.squad_limit` -- the one configured season rule
this repository enforces; a future rules engine would extend this call, not
duplicate it elsewhere. It never raises; it returns a list of issues (empty
when every squad is valid), each naming the offending `season_entry_id` and
its expected/actual squad size, for both a read-only admin check and
`close_window`'s own gate.

## Closing the window

`PreseasonRepository.close_window(season_id)`, exposed at
`POST /api/admin/preseason/{season_id}/close`:

1. locks the window row (`SELECT ... FOR UPDATE` on PostgreSQL, matching
   every other roadmap 13-15 repository's concurrency discipline) and
   refuses if it is already closed (`PreseasonWindowClosedError`, HTTP 423
   -- repeated calls fail clearly rather than silently no-oping or
   re-freezing);
2. validates every squad (`validate_squads`); if even one is invalid,
   raises `PreseasonSquadValidationError` with `.issues` identifying every
   offending entry/squad and rule, and **changes nothing** -- the window
   stays open and no snapshot is written (`tests/test_preseason.py::test_valid_squads_close_the_window_and_invalid_squads_block_it_with_diagnostics`,
   `test_all_ten_squads_must_be_valid_before_closure_succeeds`);
3. only once every squad is valid does it, in the same transaction, freeze
   version 1 of `preseason_opening_snapshot`/`preseason_opening_snapshot_entry`
   (see "Opening snapshot" below), set `closed_at`/`closed_note`, and append
   `preseason.squad.frozen` and `preseason.window.closed` audit events under
   one correlation ID.

## Opening snapshot: the frozen boundary Round 1 relies on

Closing the window does not merely stop mutating live ownership -- it also
**freezes a versioned snapshot** of exactly who owned which player at that
moment, referenced by `preseason_opening_snapshot`/
`preseason_opening_snapshot_entry`. Each entry row stores the owning
`season_entry_id`, `season_player_id`, and a reference to the exact
`player_ownership_period.ownership_period_id` that produced it -- not a copy
of player/ownership facts, only the identities/references needed to
reconstruct the boundary. This is deliberate:

- Round 1 selection validation must use `PreseasonRepository.opening_squad(season_id, season_entry_id)`
  (the *current* snapshot version) as its authoritative reference for "who
  opened the season owning this player" -- never a live re-query of
  `player_ownership_period`, which keeps changing as later history (mid-
  season roadmap 30/31 work, explicitly out of this issue's scope) is
  added.
- The snapshot is immutable at the database level (0017's triggers) and
  stable across repeated reads
  (`tests/test_preseason.py::test_closure_freezes_a_reproducible_stable_opening_snapshot`).
- Draft picks, preseason trades and the opening snapshot are three distinct,
  independently queryable layers: the snapshot is a stable *read* boundary,
  not a replacement for `app.draft`/`app.preseason` provenance. After Round
  1 begins, `DraftRepository.picks(...)`, `PreseasonRepository.list_trades(...)`/
  `trade_legs(...)` and `OwnershipRepository.history(...)` remain fully
  queryable exactly as they were the day they were written.

## Authorised corrections

`PreseasonRepository.correct_opening_snapshot(season_id, season_entry_id, remove_season_player_id=..., add_season_player_id=..., reason=...)`,
exposed at `POST /api/admin/preseason/{season_id}/correct-opening-squad`, is
a deliberately narrow, explicitly authorised escape hatch for a data-entry
error discovered *after* closure -- not a general "admin can edit anything"
mechanism, and clearly distinguishable from an ordinary trade:

| | Ordinary trade | Authorised correction |
|---|---|---|
| Requires | window **open** | window **closed** |
| Shape | any number of legs/entries | exactly one entry, one player out, one player in |
| Audit action | `preseason.trade.applied` | `preseason.correction.applied` |
| Effect on the snapshot | none (snapshot only exists after close) | appends a **new** snapshot version |
| Reason | optional | **required** (rejected otherwise) |

It still applies the underlying ownership swap through
`OwnershipRepository.release_in_transaction`/`acquire_in_transaction` -- the
same ledger a trade uses, via those methods' narrow `allow_closed_window`
parameter, which only this one call site sets. Every other invariant
(eligibility, season match, no-overlap, squad capacity) still applies in
full: an administrator cannot use this to manufacture an invalid ownership
state. The prior snapshot version's rows are never updated or deleted --
correction always **appends** `preseason_opening_snapshot` version *N+1*,
copying every unaffected entry's frozen row forward and writing only the
one corrected row, so `PreseasonRepository.snapshot_versions(season_id)`
retains every version's exact content forever.

## Rejecting ordinary edits after closure

Once a season's window is closed, `submit_trade` refuses outright
(`PreseasonWindowClosedError`). More importantly, this protection is not
only enforced at that layer: `app.player_pool.OwnershipRepository.acquire`/
`release`/`transfer` (and their `_in_transaction` counterparts) themselves
refuse any ordinary mutation once a closed `season_preseason_window` row
exists for that player/entry's season -- see
`_assert_ownership_mutation_allowed` in `app/player_pool.py`. This means
**any** caller reaching the ownership ledger directly -- a future route, an
admin script, a bypassed UI control -- is refused at the repository
boundary, not only by a UI restriction
(`tests/test_preseason.py::test_closed_window_rejects_trades_and_direct_ownership_mutation`).
A season with no window at all (still mid-draft, or predating this package)
is unaffected -- the guard only ever fires once a window exists *and* is
closed.

## API surface

All under `/api/admin/preseason/{season_id}/...`, extending issue #53's
scorer/admin surface (`/api/admin/draft/...`) rather than introducing an
unrelated second admin interface, and gated by the same `require_admin`
dependency:

| Method & path | Purpose |
|---|---|
| `GET .../status` | window state, current squad-validation issues, current opening snapshot (if any), and trade history |
| `POST .../open` | open the window (requires a finalized draft) |
| `POST .../trade` | submit an atomic trade (`legs`, optional `scorer_name`/`reason`) |
| `GET .../trades` | list every applied trade and its legs |
| `POST .../close` | validate every squad and, if all valid, freeze the opening snapshot and close |
| `GET .../opening-squad` | the current frozen snapshot's entries (optionally filtered by `season_entry_id`) |
| `POST .../correct-opening-squad` | the narrow authorised post-closure correction described above |

Every mutating endpoint is a thin translation from an HTTP request to one
`PreseasonRepository` call (see `app/routes/preseason.py`) -- no lifecycle
or ownership rule lives in the route module itself.

## Deliberate limitations / roadmap boundary

Per the issue's explicit scope: this package implements only the preseason
lifecycle above. It deliberately does **not** implement post-Round-9
delisting, mid-season trade windows, the reverse-ladder mid-season draft, or
traded draft picks -- those are roadmap packages 30-31 and depend on the
regular-season ladder existing first. `_assert_ownership_mutation_allowed`
only ever checks *this* season's preseason window; a future mid-season
window package will need its own explicit state and its own guard, not a
relaxation of this one.

## Running the tests

```
cd bbbffl_app
pytest tests/test_preseason.py tests/test_preseason_api.py tests/test_preseason_postgresql.py
```

`test_preseason_postgresql.py` skips unless `BBBFFL_DATABASE_URL` points at
a real PostgreSQL database (see `docs/database-migrations.md`), matching
`test_draft_postgresql.py`'s convention.

## Migration

`migrations/versions/0017_preseason_trades.py` (revision `0017_preseason`,
`down_revision=0016_draft_ops`) adds `season_preseason_window`,
`preseason_trade`/`preseason_trade_leg`, and
`preseason_opening_snapshot`/`preseason_opening_snapshot_entry`, with the
same append-only immutability triggers roadmap 13/14 established for draft
history. It adds no ownership table of its own -- every trade and the
opening snapshot still reference `player_ownership_period` directly.
Downgrade refuses if any window, trade or snapshot history exists, the same
"would lose real history" guard every other roadmap 13-17 migration uses.

## Running the 2026 replay

```
cd bbbffl_app
python -m scripts.replay_2026_draft --entries 10 --squad-limit 4 --auto-complete
python -m scripts.replay_2026_preseason --database-url sqlite:///$(pwd)/data/replay-2026-draft.db --season-id <season_id>
```

The first command (extended by this package with `--auto-complete`) seeds a
synthetic ten-entry draft and runs it through to finalisation entirely in
software, without requiring the admin board's manual picks. The second
seeds one illustrative synthetic trade, validates every opening squad, and
closes the window -- printing the frozen opening squad for inspection. Both
scripts are software simulations only (see `scripts/replay_2026_draft.py`'s
module docstring); neither represents real 2026 AFL player data or league
decisions, and both refuse to run when `BBBFFL_ENVIRONMENT=production`.
