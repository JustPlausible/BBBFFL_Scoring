# Scorer-operated preseason draft workflow

**Issue:** [#53 — Build scorer-operated preseason draft workflow](https://github.com/JustPlausible/BBBFFL_Scoring/issues/53)
(roadmap work package **14**, `docs/roadmap/2027-season-roadmap.md`)

**Depends on:** roadmap package 13's authoritative draft-order/pick-ownership
ledger (`app/draft.py`, [`draft-ledger.md`](draft-ledger.md)) and the
append-only audit boundary from issue #17 ([`audit-events.md`](audit-events.md)).

## What this is

A scorer/admin operator surface for running a complete BBBFFL preseason
draft, capable of running all ten entries through to finalisation. It is
deliberately an **operator surface over the existing authoritative
`DraftRepository`** — every rule about whose turn it is, which players are
eligible/available, squad capacity, and concurrency lives in `app/draft.py`
and `app/player_pool.py`, inside one database transaction per pick. Nothing
in `app/routes/draft.py` or `templates/draft.html`'s JavaScript decides any
of that; the browser only renders whatever the last `GET .../board` call
returned and submits requests, and every request rebuilds its response
from the database. A reload always reflects authoritative persisted state.

## Configuring and starting a draft

A draft is season-scoped and reuses roadmap 04-13's identity/ownership
model — there is no separate draft-only entry or player concept:

1. Create the season (`SeasonRepository.create_season`).
2. Create each BBBFFL entry (`IdentityRepository.create_entry`) — this is
   the "team" that will receive picks.
3. Configure the season's squad limit (`OwnershipRepository.configure_squad_limit`) —
   this becomes the draft's `target_squad_size` once accepted, and cannot
   change after acceptance.
4. Populate the season's player pool (`PlayerPoolRepository.refresh_player`)
   with every eligible, selectable player.
5. Accept the draft order for every participating entry, exactly once
   (`DraftRepository.accept_order`). This atomically materialises every
   pick for the whole draft (`entries × squad_limit` picks, snake-ordered)
   and freezes the accepted order — see [`draft-ledger.md`](draft-ledger.md)
   for the full acceptance/materialisation contract.

Today this seeding is done by whatever admin script or one-off tooling a
season setup requires (see `scripts/replay_2026_draft.py` for a worked,
synthetic example) — there is no dedicated "configure a season" HTTP
workflow yet; that is future scope, not this issue's.

Once accepted, open the scorer board at:

```
GET /admin/draft/{season_id}
```

## Scorer workflow

The board (`GET /api/admin/draft/{season_id}/board`, rendered by
`templates/draft.html`) shows:

- **draft status** — active / paused / completed (all picks in, not yet
  finalised) / finalised;
- **the accepted draft order**, highlighting whichever entry currently owns
  the outstanding pick;
- **the current pick** — round, overall number, and effective (possibly
  traded) owner;
- **upcoming picks**, each showing its effective owner and whether it has
  been traded;
- **completed picks**, each showing the selected player and effective owner;
- **available-player search** (`GET .../available-players?q=...`),
  restricted to eligible, currently-unowned season players — see
  `PlayerPoolRepository.list_available`/`search_available`.

To submit a pick, `POST /api/admin/draft/{season_id}/pick` with the
receiving entry, the player, and (optionally) a scorer name and reason.
This calls `DraftRepository.execute_pick`, which atomically:

1. re-resolves the current pick under a row lock (never trusting a
   client-supplied "it's still my turn");
2. validates the requested entry is the pick's *effective* (possibly
   traded) owner;
3. validates the player is eligible and currently unowned;
4. validates the receiving entry's squad capacity;
5. persists the pick, the player's new ownership period, and both
   append-only audit events — or rolls back every one of those writes if
   any validation fails.

### Operator-facing failures

| Situation | HTTP status | Exception |
|---|---|---|
| Player already owned/unavailable | 409 | `PlayerUnavailableError` |
| Wrong effective pick owner | 409 | `DraftTurnError` |
| Stale/already-completed turn | 409 | `DraftTurnError` / `DraftPickCompletedError` |
| Squad/roster capacity exceeded | 409 | `SquadCapacityError` |
| Draft is paused | 409 | `DraftPausedError` |
| Draft is already finalised | 423 | `DraftFinalizedError` |
| Malformed/incomplete draft order | 400 | `DraftOrderError` |
| Unknown season/pick/player ID | 404 | `KeyError` |

Every one of these maps to a JSON `{"detail": "..."}` body (see
`app/main.py`'s exception handlers) that `draft.html` surfaces as an alert
— the operator refreshes and retries rather than the UI silently
retrying or guessing.

## Proxy picks and provenance

This is a **scorer-first** implementation: coaches do not submit their own
picks yet (that is explicitly out of scope — see the issue). Every pick is
entered by a scorer/admin operator on behalf of the receiving entry.
`PickRequest.scorer_name` is optional free text recorded as the audited
actor's `actor_id` with `actor_role="scorer"` — **never** as the receiving
`season_entry_id`, which is always a separate, required field. This is what
lets `GET /api/admin/audit-events?action=draft.pick.completed` (or any
other audit query) answer both "which entry received this player" (the
domain row) and "which human scorer entered the selection" (the audit
event) independently. `app/audit.py`'s `KNOWN_ACTOR_TYPES` allowlist still
applies: there is no authenticated coach/scorer identity yet (roadmap
package 19/20), so every actor is recorded as `anonymous_operator` with a
free-text role/name, not impersonating a real authenticated identity.

## Pause/resume

`POST /api/admin/draft/{season_id}/pause` (optionally with a `reason`) and
`.../resume` persist `paused_at`/`paused_reason` directly on the
authoritative `season_draft` row, guarded by the same row-lock discipline
(`SELECT ... FOR UPDATE` on PostgreSQL) as pick execution. While paused,
`execute_pick` refuses outright (`DraftPausedError`). Because pause state
is a database column, not an in-memory flag or a browser-held value:

1. start a draft and execute some picks;
2. pause it;
3. restart the application process entirely;
4. reload `/admin/draft/{season_id}`;

resumes showing exactly the same paused state and exactly the same current
turn — no client-side reconstruction is possible or needed. See
`tests/test_draft_operations.py::test_pause_persists_across_a_fresh_connection_and_resumes_the_same_turn`
for this exact restart scenario proven against a real SQLite file on disk,
and `tests/test_draft_postgresql.py::test_postgresql_pause_waits_for_the_season_draft_row_lock`
for the PostgreSQL row-lock proof.

## Corrections / undo

`POST /api/admin/draft/{season_id}/correct` with the erroneous pick's ID
(and an optional reason) undoes it via `DraftRepository.correct_pick`.
Deliberately narrow, per the issue's guidance to prefer a constrained
mechanism over arbitrary historical rewriting:

- **Only the single most-recently-completed pick in the whole draft** may
  be corrected — there is no "correct any pick from three rounds ago"
  workflow. This sidesteps having to reconcile sequencing/ownership against
  picks completed after the one being corrected.
- The **original, erroneous `draft_pick` row is never updated or
  deleted**. Roadmap 13's immutability triggers (0015) already reject any
  ordinary `UPDATE`/`DELETE` of a completed pick's `selected_season_player_id`
  or `completed_at`, and correction does not touch them. Instead:
  - the erroneous player acquisition is **released** (not deleted) via
    `OwnershipRepository.release_in_transaction`, preserving its full
    acquired/released history;
  - a **new** `draft_pick` row is inserted for the same slot (same
    `overall_number`/round/owner), uncompleted, becoming the current pick
    again;
  - the original row's `superseded_by_draft_pick_id` pointer (the one
    column migration 0016 deliberately left outside the immutability
    triggers' protected column list) is set to that new row;
  - a `draft_pick_correction` row records the original pick, its
    replacement, when, by whom (via the correlated audit event), and why —
    and is itself append-only (its own immutability trigger).
- The scorer then re-selects through the **ordinary** `execute_pick` path
  — there is no separate "corrected selection" code path, so every
  validation (eligibility, capacity, turn ownership) applies exactly as it
  would for any other pick.

`DraftRepository.picks(season_id)` (the board's normal view) only ever
returns the one active row per slot; pass `include_superseded=True` to see
every historical attempt, and `DraftRepository.corrections(season_id)` to
list the correction ledger itself. See
`tests/test_draft_operations.py`'s correction tests and
`tests/test_draft_postgresql.py::test_postgresql_correction_reopens_slot_and_preserves_the_original_row`
for the full before/after proof, including that the immutability trigger
still rejects a direct rewrite of the original row afterwards.

## Traded picks

`DraftRepository.transfer_pick` (roadmap 13) reassigns a pick's *effective*
owner without moving the pick or losing its original allocation. Every pick
view returned by the board API carries both `original_season_entry_id`/
`original_team_name` and `current_season_entry_id`/`current_team_name`,
plus a `traded` flag — so the scorer can always see who a pick was
originally allocated to as well as who currently holds it, without losing
the underlying pick's stable identity (`draft_pick_id`). Execution always
validates against the *current* (effective) owner.

## Finalisation

Draft **completion** (every configured pick has a selection) and
**finalisation** are separate concepts — the draft is never automatically
finalised just because the last pick was entered. `POST
/api/admin/draft/{season_id}/finalize` (`DraftRepository.finalize`) is
blocked (`DraftNotCompleteError`, 409) unless:

- every configured pick is completed, and
- every entry's resulting squad exactly matches the configured squad size
  (a defence-in-depth check — `execute_pick`'s own squad-capacity
  validation already makes this the only reachable outcome once every pick
  is completed, but finalisation re-verifies it explicitly rather than
  trusting that invariant blindly).

Once finalised, `execute_pick`, `pause`, and `correct_pick` all refuse
outright (`DraftFinalizedError`, HTTP 423) — ordinary controls stop
mutating the draft, while `GET .../board` continues to serve the complete,
unchanged pick history and squad/ownership state.

### Reopening a finalised draft

This is deliberately **not** an ordinary one-click control. `POST
/api/admin/draft/{season_id}/reopen` requires both a `reason` and a literal
`confirm` field equal to `"REOPEN FINALIZED DRAFT"` — a mismatch is
rejected (400) before anything is touched. `draft.html` exposes this only
inside a collapsed "danger zone" section, never alongside the normal
finalise button. `DraftRepository.reopen` itself also refuses an empty
reason. Reopening clears `finalized_at`/`finalized_note` and appends a
`draft.reopened` audit event recording who and why; ordinary controls
(including a fresh `finalize`) work normally again afterwards.

## Concurrency and stale-turn behaviour

Two scorer/browser sessions can never both execute the same pick. Every
mutating `DraftRepository` command (`execute_pick`, `pause`, `resume`,
`correct_pick`, `finalize`, `reopen`) runs inside one transaction that
locks the relevant row(s) — `season_draft` via
`SELECT ... FOR UPDATE` (`_locked_draft`), and the specific `draft_pick`
row being completed/corrected — using the repository's established
`app.db._for_update_suffix` convention (a no-op on SQLite, which instead
takes its single writer lock ahead of validation for development/tests, as
every other roadmap 13 repository already does). A losing concurrent
attempt fails immediately with a clear conflict (`DraftTurnError`,
`DraftPickCompletedError`, or a database `IntegrityError`/`OperationalError`
surfaced the same way) — it never silently retries against the new turn,
and it never accidentally completes the requested player against a later
pick. See:

- `tests/test_draft.py::test_competing_execution_completes_one_pick_and_player_once`
  (SQLite, two threads racing the same pick);
- `tests/test_draft_postgresql.py::test_postgresql_two_concurrent_attempts_at_the_same_pick_exactly_one_succeeds`
  (the same race against real PostgreSQL row locking);
- `tests/test_draft_postgresql.py::test_postgresql_pause_waits_for_the_season_draft_row_lock`
  (proving `pause()` queues behind a held `season_draft` row lock rather
  than racing it).

## Migrations

`migrations/versions/0016_draft_operations.py` (revision `0016_draft_ops`,
`down_revision=0015_draft`) adds, on top of roadmap 13's schema:

- `season_draft.paused_at` / `paused_reason` / `finalized_at` /
  `finalized_note` (all nullable);
- `draft_pick.superseded_by_draft_pick_id` (nullable, self-referencing,
  deliberately outside 0015's immutability triggers' protected column
  list) plus a partial unique index (`uq_draft_pick_active_sequence`,
  `WHERE superseded_by_draft_pick_id IS NULL`) replacing the old plain
  `(draft_id, overall_number)` uniqueness, so exactly one row per slot is
  ever "active" while every earlier attempt for that slot survives
  forever;
- the append-only `draft_pick_correction` table (with its own
  reject-`UPDATE`/`DELETE` trigger, mirroring `audit_event`'s).

The FK from `superseded_by_draft_pick_id` to `draft_pick.draft_pick_id` is
declared `DEFERRABLE INITIALLY DEFERRED`: `correct_pick`'s transaction must
mark the original row superseded *before* its replacement row exists (so
the partial unique index is never transiently violated by two active rows
for the same slot), which means that FK reference is written before its
target exists — resolved at commit, once the replacement row has been
inserted. PostgreSQL supports this natively; SQLite additionally requires
the correction transaction to issue `PRAGMA defer_foreign_keys = ON` (see
`DraftRepository.correct_pick`), which resets automatically at commit.

Downgrade refuses if any correction has ever happened, or if any draft is
currently finalised — both would otherwise be silently discarded.

## Running the tests

```
cd bbbffl_app
pytest tests/test_draft.py tests/test_draft_operations.py tests/test_draft_postgresql.py tests/test_scorer_draft_workflow.py
```

`test_draft_postgresql.py` skips unless `BBBFFL_DATABASE_URL` points at a
real PostgreSQL database (see `docs/database-migrations.md`); CI's
`postgres-migrations` job runs it against a real PostgreSQL 16 service
alongside the repository's other PostgreSQL-only concurrency suites.

The full synthetic ten-entry draft
(`tests/test_scorer_draft_workflow.py::test_full_ten_entry_draft_runs_through_finalisation_via_the_admin_api`)
drives the entire workflow through the real HTTP admin API (FastAPI's
`TestClient` against the actual `app.main.app`, not a shortcut through the
repository layer) against its own isolated, temporary SQLite database: a
full ten-entry draft, a rejected wrong-turn pick, a rejected already-owned
player, proxy provenance, pause/resume, a correction and re-selection, a
reciprocal trade, a blocked early finalisation, completion, explicit
finalisation, rejected post-finalisation mutation, and a fully-gated
reopen. It never touches the application's default database.

## Running the 2026 replay

```
cd bbbffl_app
python -m scripts.replay_2026_draft --entries 10 --squad-limit 4
BBBFFL_DATABASE_URL=sqlite:///$(pwd)/data/replay-2026-draft.db uvicorn app.main:app --reload
# open the printed /admin/draft/<season_id> URL
```

This seeds a brand-new, isolated season and a synthetic player pool (real
2026 AFL player data is not available to this environment — see
`docs/afl-evidence-fixtures.md`'s "Live validation status" — so the
replay pool is clearly labelled, generated content, never presented as
real historical BBBFFL evidence) into its own SQLite file, distinct from
the application's normal development database, and refuses to run at all
when `BBBFFL_ENVIRONMENT=production`. Every run creates a fresh season, so
re-running it never collides with or contaminates a prior replay.

## Deliberate limitations / follow-up work

- **Scorer-first only.** No coach-facing self-service pick submission
  exists; every pick is scorer-entered. Introducing that is explicitly out
  of this issue's scope.
- **No dedicated "configure a season for drafting" HTTP workflow.** Season/
  entry/player-pool/squad-limit setup remains script/admin-tool driven
  (see `scripts/replay_2026_draft.py` for a worked example); only the
  draft-execution workflow itself is HTTP-routed here.
- **Correction is single-most-recent-pick only.** Rewriting an arbitrary
  historical pick is deliberately not supported — see "Corrections / undo"
  above for why, and the issue's explicit preference for a narrowly
  constrained mechanism.
