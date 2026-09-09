# Authoritative 2026 second-half replay playbook (issue #165)

## A. Purpose and scope

This is the authoritative operational playbook for the 2026 second-half
historical replay: BBBFFL Round 10, the reconstructed mid-season draft, then
Rounds 11–20 through the home-and-away season boundary. It picks up exactly
where [`2026-first-half-replay-playbook.md`](2026-first-half-replay-playbook.md)
left off (Rounds 1–9, all `final`) and hands off into the later
finals-seeding/finals/SuperScore phase, which this document does not cover.

The confirmed historical 2026 chronology is: Round 10 completes using the
pre-mid-season-draft squads, then the mid-season draft runs, then Round 11
onward uses the post-draft squads. Issue
[#166](https://github.com/JustPlausible/BBBFFL_Scoring/issues/166) executes
the Round 10 replay against the working database this playbook establishes;
sections F–G below are that issue's operational script. `docs/
replay-checkpoint-2026.md`'s "Operator procedure" section documents the
underlying weekly round mechanics reused unchanged in sections F and J.

**Core rule, stated once and never relaxed below:** the second-half replay
never bootstraps a fresh competition. Every step in this document operates
on a *copy* of the completed, locked first-half database. The original
first-half checkpoint is never started, mutated, or deleted by anything in
this playbook.

## B. Database continuity requirement (read first)

- The completed first-half replay database is the single source of truth
  for 2026 competition history through Round 9: season configuration, draft
  state, squads/ownership, weekly submissions, results, ladder history,
  audit/event history, and the existing replay evidence in
  `docs/evidence/2026-first-half-replay/`.
- That source database/checkpoint pair is retained **unchanged**, as an
  immutable historical checkpoint and recovery artefact, for the entire
  second-half replay and beyond.
- The second-half replay operates against a **separate working copy** of
  that database. The working copy starts with every one of the items above
  already present — it is a continuation, not a re-bootstrap.
- The completed 2026 database (first-half source plus the second-half
  working copy carried through to season close) is intended to be retained
  as historical league evidence once the application is prepared for the
  2027 live season. Preparing 2027 (a new season row, new rules revision,
  new fixture draw) must not require destroying, truncating, or overwriting
  the completed 2026 dataset — 2027 season setup creates new season-scoped
  rows (see `docs/season-competition-schema.md`) alongside the retained 2026
  rows, in the same database or in a database seeded from a copy of it,
  never in place of it.

## C. Verify and preserve the first-half checkpoint

1. **Verify.** Confirm the operator holds the Phase 1 closing recovery
   package described in
   [`docs/evidence/2026-first-half-replay/phase-one-closeout.md`](evidence/2026-first-half-replay/phase-one-closeout.md):
   a PostgreSQL custom-format database archive, its matching replay
   checkpoint JSON, and a SHA-256 record for both. Confirm the archive's
   catalogue with `pg_restore --list <archive>` and parse the checkpoint
   JSON. Confirm both match the recorded closing baseline in
   [`evidence-manifest.md`](evidence/2026-first-half-replay/evidence-manifest.md):
   application commit `3abc503`, migration `0026_lineup_adjudication`,
   checkpoint `2026-05-10T10:15:00Z` at stage `final-results`, BBBFFL Rounds
   1–9 all `final` with ten authoritative submissions each.
2. **Record identity/provenance.** Copy the table in
   [`docs/evidence/2026-second-half-replay/provenance-manifest.md`](evidence/2026-second-half-replay/provenance-manifest.md)
   and fill in the source archive's filename (private, not committed), its
   SHA-256, and the checkpoint time/stage above.
3. **Preserve unchanged.** If the `bbbffl-2026-first-half` installation
   (`compose.first-half-replay.yaml`, project `bbbffl-2026-first-half`,
   volume `first-half-database`) is still running, take one final
   confirmation snapshot without mutating it, then stop it — never
   `down -v` this project:

   ```bash
   FIRST='docker compose -p bbbffl-2026-first-half -f compose.first-half-replay.yaml'
   $FIRST exec -T database pg_dump -U bbbffl -Fc bbbffl_2026_first_half \
     > replay/2026-first-half/backups/closeout-reconfirmation.dump
   $FIRST stop
   ```

   The archive and checkpoint from step 1 remain the authoritative source;
   this reconfirmation snapshot is a belt-and-braces duplicate, not a
   replacement. Retain both privately (see `evidence-manifest.md`'s
   "Excluded repository evidence" list — database dumps and checkpoints are
   never committed).

## D. Create the second-half working copy

The app service is deliberately not started in this section. `BBBFFL_AFL_
MODE=replay` makes application startup (`app.main`'s lifespan handler)
eagerly construct a `ReplayAflDataSource` against `BBBFFL_AFL_REPLAY_
EVIDENCE_PATH`, which fails closed if that file does not exist — and section
E below has not acquired it yet at this point in the playbook. Section F's
opening step brings the app up, once that evidence exists.

1. **Stand up an empty second-half installation.** This installation is
   deliberately named `bbbffl-2026-second-half`, listens at
   <http://localhost:8019/login>, uses its own `second-half-database`
   project volume, and bind-mounts `replay/2026-second-half/{evidence,state,
   logs,backups,config}`. It cannot reuse the first-half database or volume.
   Create the bind-mount directories explicitly first — `$SECOND config`/
   `build`/`up -d database` do not create them, since they belong to the
   `app` service, not `database`:

   ```bash
   mkdir -p replay/2026-second-half/{evidence,state,logs,backups,config}
   cp bbbffl_app/.env.second-half-replay.example bbbffl_app/.env.second-half-replay
   # Set unique session/admin/operator secrets; set no live AFL endpoint.
   SECOND='docker compose -p bbbffl-2026-second-half -f compose.second-half-replay.yaml'
   $SECOND config                       # inspect names and exact mounts
   $SECOND build
   $SECOND up -d database
   ```

2. **Restore the preserved first-half checkpoint into it.** Use the private
   custom-format archive verified in section C (a plain-SQL per-round backup
   such as `after-round-9.sql` is an acceptable fallback if the custom-format
   archive is unavailable — see the alternative command below):

   ```bash
   cat /path/to/private/phase1-closing-archive.dump \
     | $SECOND exec -T database pg_restore -U bbbffl -d bbbffl_2026_second_half \
       --clean --if-exists --no-owner
   ```

   Plain-SQL fallback:

   ```bash
   cat replay/2026-first-half/backups/after-round-9.sql \
     | $SECOND exec -T database psql -U bbbffl -d bbbffl_2026_second_half
   ```

3. **Initialise a fresh second-half checkpoint — do not copy the first-half
   `checkpoint.json` verbatim.** It carries first-half `finalised_round_ids`
   (AFL rounds 1343–1352, per `phase-one-closeout.md`), and once section E's
   evidence package exists (AFL rounds mapped to BBBFFL Rounds 10–20, not
   1343–1352), `ReplayAflDataSource._load` rejects any `finalised_round_ids`
   entry absent from the loaded evidence (`app/replay.py`) — a copied
   first-half checkpoint would fail validation and application startup as
   soon as the new evidence is in place. `scripts/first_half_replay.py`'s
   `checkpoint` subcommand is itself evidence-agnostic (it only reads/writes
   the checkpoint JSON at `--state`, never the evidence file), so it is
   reused here to create a genuinely new checkpoint at the target path
   instead — starting from the preserved first-half closing effective time,
   with nothing yet finalised against the new evidence (the `state`
   directory already exists from step 1; the app service is not started
   here — see above):

   ```bash
   $SECOND run --rm -v "$PWD/bbbffl_app:/app" \
     -v "$PWD/replay/2026-second-half/state:/replay/state" app \
     python -m scripts.first_half_replay checkpoint --state /replay/state/checkpoint.json \
     --effective-at 2026-05-10T10:15:00Z --stage scheduled
   ```

4. **Confirm the migration baseline.** This runs the migrator as a one-off
   container, not the long-running app service, so it needs no evidence
   file. The restored database was already at the first-half closing
   migration; running the current application's migrator against it must be
   a no-op upgrade to the current head (`0027_midseason_draft` as of this
   playbook, which carries no schema this playbook's Round 10 steps depend
   on — see section H):

   ```bash
   $SECOND run --rm -v "$PWD/bbbffl_app:/app" app python -m app.migrations current
   $SECOND run --rm -v "$PWD/bbbffl_app:/app" app python -m app.migrations upgrade
   $SECOND run --rm -v "$PWD/bbbffl_app:/app" app python -m app.migrations current
   ```

5. **Record the working-copy identity and provenance** in
   `docs/evidence/2026-second-half-replay/provenance-manifest.md`: the
   restored database's own new checkpoint file hash, the application commit
   (`git rev-parse HEAD`) and confirmed migration head from step 4, and an
   explicit `restored_from` pointer back to the source archive's SHA-256
   recorded in section C step 2. This is the provenance chain the acceptance
   criteria require between the first-half checkpoint and the second-half
   working database.
6. **Take a fresh pre-Round-10 backup** of the working copy immediately, the
   same way section G below backs up every later round, before any Round 10
   action:

   ```bash
   $SECOND exec -T database pg_dump -U bbbffl -Fc bbbffl_2026_second_half \
     > replay/2026-second-half/backups/pre-round-10.dump
   cp replay/2026-second-half/state/checkpoint.json \
      replay/2026-second-half/backups/checkpoint-pre-round-10.json
   ```

## E. AFL evidence acquisition for Rounds 10–20

`app.replay_acquisition.acquire_second_half_2026` (driven by `scripts.
second_half_replay acquire`, issue #174) resolves the 2026 AFL season from
AFL-api metadata — never a hard-coded database ID — and requires exactly
AFL Rounds 10–20 inclusive (eleven rounds). It validates that exact set and
fails closed on any round missing, duplicated, or ambiguous, on any
match/player-stat identity acquired twice, and on incomplete final-stat
coverage, before it writes anything. It shares its season-resolution,
player-pool pagination, and per-round match/stat/roster acquisition
boundaries with `acquire_first_half_2026` (`app/replay_acquisition.py`'s
`_resolve_2026_season_and_players`/`_acquire_match_evidence` helpers) — the
same acquisition/domain boundary the first half uses, not a parallel
implementation. Do not accept a package validated against a round count
other than eleven; that would silently admit a missing or extra round and
fail later at preflight or replay instead of here.

Unlike the first-half acquisition (`2026-first-half-replay-playbook.md`
section C, run from a host virtualenv), every second-half acquisition/
validation command below runs through Docker — this playbook's operator
uses Docker throughout because host Python versions are not treated as
authoritative. `compose.second-half-replay.yaml` mounts
`replay/2026-second-half/evidence` and `.../state` read-only for the
long-running `app` service (section D); the one-off commands below
override those two mounts read-write for the duration of the command only,
exactly like section D step 3 already does for the checkpoint state mount
when initialising it.

`second_half_replay acquire` deliberately has **no** `--player-pool-output`
flag (unlike `first_half_replay acquire`). The second-half working copy
already carries the verified season-wide `2026-player-pool.json` acquired
during the first half (section B); nothing in this playbook re-bootstraps a
season player pool from a file for the second half, so acquiring Round
10–20 evidence never touches, rebuilds, or redefines it. A genuine need to
refresh season membership is a separate, explicitly justified action, never
a side effect of this command.

1. **Acquire.** Requires the configured consumer API and `AFL_API_KEY`,
   exactly like the first-half acquisition. Do not prepare Bruno files —
   the command follows every player-pool page and every round/match/stat
   endpoint itself, exactly as `2026-first-half-replay-playbook.md` section
   C describes for the first half:

   ```bash
   read -rsp 'AFL API key: ' AFL_API_KEY; echo
   $SECOND run --rm \
     -v "$PWD/bbbffl_app:/app" \
     -v "$PWD/replay/2026-second-half/evidence:/replay/evidence" \
     -e AFL_API_BASE_URL=https://<consumer-api-host> \
     -e AFL_API_KEY="$AFL_API_KEY" \
     app \
     python -m scripts.second_half_replay acquire \
     --output /replay/evidence/2026-second-half.json
   unset AFL_API_KEY
   ```

2. **Validate.** The fresh second-half checkpoint from section D step 3
   must already exist at this point (it is evidence-agnostic and does not
   require the acquired package). The two `:ro` mounts below simply make
   explicit the read-only access `compose.second-half-replay.yaml` already
   declares for these paths — no override needed here, unlike acquire:

   ```bash
   $SECOND run --rm \
     -v "$PWD/bbbffl_app:/app" \
     -v "$PWD/replay/2026-second-half/evidence:/replay/evidence:ro" \
     -v "$PWD/replay/2026-second-half/state:/replay/state:ro" \
     app \
     python -m scripts.second_half_replay validate \
     --evidence /replay/evidence/2026-second-half.json \
     --state /replay/state/checkpoint.json
   ```

   Expect validation `PASS`, season 2026, the **eleven** included AFL round
   identities for rounds 10–20 inclusive, every match with stat coverage,
   and explicit available/unavailable roster coverage. A missing optional
   roster is diagnostic, not fabricated. Missing/incomplete stats,
   malformed scheduled starts, an unsupported package version, a season
   other than 2026, a round count other than eleven, or any duplicate/
   conflicting match or round identity is fatal and reported by
   `validate_replay_package` before anything reads as a pass.

3. **Confirm coverage/manifest diagnostics.** The `validate` command's PASS
   output already reports round/match/stats/roster coverage; to inspect the
   full manifest (acquisition timestamp, source host — never credentials —
   API/exporter versions, included round identities, `player_pool_count`/
   `player_pool_page_count`), read the evidence file directly on the host —
   it is a plain JSON file at the bind-mounted path, no container needed:

   ```bash
   jq '.manifest' replay/2026-second-half/evidence/2026-second-half.json
   ```

   Confirm `manifest.included_rounds` lists exactly round numbers 10
   through 20 with no repeats, `manifest.match_count` equals
   `manifest.player_stat_match_count`, and `manifest.package_version` is
   `bbbffl.second-half/v1`.

4. **Hermetic proof: disconnect AFL-api.** Now stop/disconnect `afl-api`
   (or firewall the host). Leave it unavailable for every remaining step.
   Second-half replay never falls back to it — once acquisition has
   written `2026-second-half.json` and validation has passed, nothing later
   in this playbook depends on AFL-api being reachable. `scripts.
   second_half_replay validate`, and the application startup in section F,
   only ever read the local evidence/checkpoint files.

5. **Proceed to the pre-Round-10 startup/checkpoint validation.** Section F
   step 1 brings up the `app` service now that the evidence package exists
   and the fresh checkpoint from section D step 3 is in place, then section
   F step 2 confirms Rounds 1–9 history survived the restore before Round
   10 itself begins.

## F. Bring up the application and replay Round 10 (pre-mid-season-draft squads)

1. **Bring up the app now that the evidence package exists**, and confirm it
   starts against the restored data:

   ```bash
   $SECOND up -d app
   $SECOND ps
   curl --fail http://localhost:8019/health
   ```

2. **Confirm prior history survived the copy.** Before touching anything,
   verify Rounds 1–9 still read `final` with ten submissions each (Season
   Centre / Round Centre), the Round 9 ladder matches
   [`round-results.md`](evidence/2026-first-half-replay/round-results.md)'s
   closing ladder table, squads/ownership match the first-half closing
   squads, and the first-half audit history is present. A mismatch here is a
   restore defect — stop and re-restore from the verified source (section D)
   rather than proceeding.

Round 10 itself is replayed **exactly like an ordinary round of Rounds
1–9** — follow `2026-first-half-replay-playbook.md` section G's
fourteen-step canonical procedure unchanged, against the second-half
installation (`$SECOND` in place of `$FIRST`, `replay/2026-second-half/...`
in place of `replay/2026-first-half/...`), using Round 10's own AFL mapping
and evidence. This document does not re-list those fourteen steps; only the
Round-10-specific boundary is called out here and in section G.

Make especially clear to whoever executes this:

- Round 10 uses the **pre-mid-season-draft squads** — the exact ownership
  state restored from the first-half checkpoint. No delisting, trade, or
  draft action of any kind happens before Round 10 reaches `final`.
- If a historical result or statistic needs correction, use the existing
  audited correction mechanisms (`docs/scorer-round-review.md`'s official-
  result correction, or the locked-lineup correction workflow described in
  `2026-first-half-replay-playbook.md` section J) exactly as Rounds 1–9 did.
  Never hand-edit ladder order or a persisted result row to force agreement.
- Round Preflight and Weekly Lineup behave identically to Rounds 1–9;
  Opening Round exceptions no longer apply (Round 10 is well past every
  configured compensating-bye round in the first-half evidence), but verify
  this rather than assuming it.

## G. Finalise Round 10 and checkpoint before the draft

1. Publish all five Round 10 results through the normal Scorer Round Review
   sign-off, exactly as for Rounds 1–9.
2. Verify the Round 10 ladder in Season Centre/Round Centre: confirm it
   orders by competition points, percentage, then PF (exact equality is an
   audited Scorer decision, never an invented tiebreaker — see
   `docs/ladder-progression.md`), and record it in
   `docs/evidence/2026-second-half-replay/round-results.md`.
3. Back up the database and checkpoint, matching the pattern in section D
   step 7:

   ```bash
   $SECOND exec -T database pg_dump -U bbbffl -Fc bbbffl_2026_second_half \
     > replay/2026-second-half/backups/after-round-10.dump
   $SECOND logs --no-color > replay/2026-second-half/logs/after-round-10.log
   cp replay/2026-second-half/state/checkpoint.json \
      replay/2026-second-half/backups/checkpoint-after-round-10.json
   ```

4. **This backup is the verified pre-draft checkpoint** issue #166's
   acceptance criteria require. Record its identity (backup filename,
   private SHA-256, checkpoint timestamp/stage, confirmed Round 10 ladder
   snapshot) in `provenance-manifest.md` before any delisting, trade, or
   draft action begins. No mid-season draft action is permitted until this
   checkpoint exists and Round 10 reads `final`.

## H. Conduct the reconstructed mid-season draft

The mid-season draft's full technical design lives in
[`docs/midseason-draft.md`](midseason-draft.md) (what was built) and
[`docs/midseason-draft-planning.md`](midseason-draft-planning.md) (the
agreed competition-process specification, see its "2026 replay scope"
section). This playbook does not repeat that design; it lists the
procedural checkpoints the replay operator drives through `scripts/
replay_2026_midseason_draft.py`, one real `app.midseason_draft.
MidseasonDraftRepository` action at a time, exactly as the script's own
module docstring describes. Every command below is run through this
`msd` shell function:

```bash
msd() {
  $SECOND run --rm -v "$PWD/bbbffl_app:/app" app \
    python -m scripts.replay_2026_midseason_draft --database-url <second-half-database-url> "$@"
}
```

The checkout must be mounted (`-v "$PWD/bbbffl_app:/app"`) for this to
work: `bbbffl_app/Dockerfile` deliberately does not copy `scripts/` into
the built image, only `app`/`migrations`/`data` — the same reason section
D's migration commands mount the checkout too. Run this from the repository
root (so `$PWD/bbbffl_app` resolves correctly), or invoke the script
directly against the Postgres connection string from the host instead.

**Known prerequisite: new-to-the-pool AFL players are not automatically
draftable.** `msd pick`/`msd trade` operate on an existing
`season_player_id` in the working database's `season_player_pool` table,
which the working copy inherited from the first-half bootstrap and this
playbook's acquisition (section E) never rebuilds or extends (see section
E's player-pool-handling note). If the second-half evidence's acquired
`players` list (issue #174) contains a genuine 2026 AFL season member who
has no corresponding `season_player_pool` row — most plausibly a player
signed/listed after the first-half capture — no supported command in this
playbook yet reconciles that gap before a historical mid-season-draft
selection of them. `app.player_pool.PlayerPoolRepository.refresh_player`
is the existing upsert `replay_bootstrap.py` itself uses for exactly this
purpose at first-half bootstrap time and would be the natural mechanism to
reuse, but no operator-facing command currently exposes it for a bulk
reconciliation against an acquired evidence file outside that bootstrap
flow. Confirm, before running `msd pick` for any given historical
selection, that the selected player already has a `season_player_pool` row
for this season; if not, this is a genuine blocking prerequisite for that
pick — track it against issue #166/#168 (or a dedicated follow-up issue)
rather than inventing a reconciliation step here or silently rebuilding/
redefining the established `2026-player-pool.json`/`season_player_pool`
source of truth. This playbook implementing the mid-season draft itself is
explicitly out of scope for issue #174.

1. **Configure/verify the trigger round.** `set-trigger-round` records the
   BBBFFL round after which the draft occurs; it has no default, so a
   season bootstrapped before this column existed (the 2026 replay) always
   starts unset. Set it to 10 and confirm with `status`:

   ```bash
   msd set-trigger-round --season-id <season_id> --trigger-round 10
   msd status --season-id <season_id>
   ```

2. **Confirm/freeze the Round 10 ladder basis.** `confirm-ladder` requires
   every round through the trigger round to be `final` (true after section
   G) and freezes an immutable copy of the calculated ladder into
   `midseason_ladder_snapshot`, seeding the draft order from its reverse
   (last place picks first, ties broken by `season_entry_id`). The *live*
   ladder is never locked or mutated by this step — verify the frozen order
   against the checkpointed Round 10 ladder from section G step 2 before
   proceeding:

   ```bash
   msd confirm-ladder --season-id <season_id> --competition-id <competition_id>
   ```

   If the draft order needs an audited exception (a genuine tie
   resolution, or a confirmed historical override), use
   `override-order --reason "..."` — it replaces `midseason_draft_order`
   under an audited event and never touches the frozen snapshot.
3. **Open the delisting/trading window** and **reconstruct relevant
   delistings and trades from available evidence.** Every action is
   recorded as an `anonymous_operator` proxy with a substantive reason,
   exactly matching the existing draft/preseason proxy convention — never
   invent a delisting or trade with no supporting evidence:

   ```bash
   msd open-delisting-window --season-id <season_id>
   msd delist --season-id <season_id> --season-entry-id <entry> --season-player-id <player> --reason "..."
   msd trade --season-id <season_id> \
     --leg player:<from_entry>:<to_entry>:<season_player_id> \
     --leg pick:<from_entry>:<to_entry>:<draft_round> --reason "..."
   msd decide-trade --season-id <season_id> --trade-id <trade_id> --approve --reason "..."
   ```

   `reverse-trade` (`reverse_trade_approval`) is the audited correction for
   an approved trade decided in error, while the window is still open; it
   is not a way to undo a trade after `lock-delistings`.
4. **Resolve every pending trade before lock.** `lock-delistings` refuses
   outright while any trade for this draft is still `pending`
   (`MidseasonPendingTradesError`) — decide or withdraw every open trade
   first.
5. **Lock delistings.** This releases every still-active delisted player's
   ownership into the available pool and marks every active delisting
   locked. It also re-plans the eventual selection allocation before
   committing and refuses (`MidseasonPickReconciliationError`) if any
   entry's final selection count would not match its vacancies, an
   approved pick leg has no vacancy to apply to, or any entry's live squad
   size is still over the configured limit — read the exception's
   `.mismatched`/`.unapplied_leg_ids`/`.overfull` detail rather than
   forcing past it:

   ```bash
   msd lock-delistings --season-id <season_id>
   ```

6. **Generate selections and build the available-player pool.**
   `generate-selections` computes each entry's vacancy count, applies any
   approved pick-trade legs, and materialises the numbered pick table. If
   no entry has any vacancy at all, this is a valid trivial completion
   (straight to `draft_complete`, no picks to make) — not an error:

   ```bash
   msd generate-selections --season-id <season_id>
   ```

7. **Execute/reconstruct picks** in generated order, one real historical
   selection at a time:

   ```bash
   msd pick --season-id <season_id> --season-entry-id <entry> --season-player-id <player> --reason "..."
   msd status --season-id <season_id>
   ```

   Use `auto-complete` only once no further historical evidence exists for
   the remaining picks — it is a deliberately synthetic convenience, always
   logged as `SIMULATION`, and must never override a known selection. If a
   pick is entered incorrectly, **do not** use the ordinary scorer draft
   correction workflow (`docs/scorer-draft-workflow.md`): its endpoint calls
   `DraftRepository.correct_pick` with the default `draft_kind="preseason"`,
   and once a mid-season draft exists this season carries both a preseason
   and a mid-season draft, so it would look for the pick under the wrong
   `draft_kind` and fail. Use the mid-season-specific correction instead,
   reopening first if the draft has already reached `draft_complete` — its
   final selection auto-finalises the underlying engine draft, and
   `correct_pick` refuses outright once `finalized_at` is set, so
   `reopen-draft` must run *before* `correct-selection`, not after:

   ```bash
   msd reopen-draft --season-id <season_id> --reason "..."   # only if already draft_complete
   msd correct-selection --season-id <season_id> --draft-pick-id <pick_id> --reason "..."
   ```

   `correct_pick` is deliberately narrow: it only ever corrects the single
   most-recently-completed active pick in the draft, precisely so a
   correction never has to reconcile sequencing against picks completed
   after it. If the wrong pick is discovered only after later picks have
   already been made, either unwind those later picks first — repeat
   `correct-selection` back to (and including) the wrong one, in reverse
   pick order, then re-`pick` each slot correctly forward again — or, if
   that is too disruptive to reconstruct cleanly, restore the working
   database from the Round 10/pre-draft checkpoint (section G) and redo
   section H from `set-trigger-round`/`confirm-ladder` onward.
8. **Apply any audited exceptional Scorer correction** here — an
   `override-order` reason, a `reverse-trade`, or a draft-pick correction —
   and record each one in `docs/evidence/2026-second-half-replay/
   round-results.md`'s exceptional-workflow section, matching the
   first-half evidence's own "Exceptional workflow evidence" style.
9. **Verify final squad sizes** and draft-audit state: `status` reports
   completed vs. total picks; every entry's squad should sit at the
   configured limit once `draft_complete` is reached automatically after
   the final required selection. If completion did not transition cleanly
   (e.g. an interrupted run), `reconcile-completion` retries it safely —
   it is a no-op if there is nothing pending.
10. **Close the post-draft phase when appropriate.** Post-draft player
    trades remain available until explicitly closed; close them once no
    further historical post-draft trade is expected, before Round 11 opens:

    ```bash
    msd close-post-draft-trading --season-id <season_id> --reason "..."
    ```

## I. Post-draft checkpoint

Back up the database and checkpoint the same way as section G step 3, named
`after-midseason-draft` instead of a round number:

```bash
$SECOND exec -T database pg_dump -U bbbffl -Fc bbbffl_2026_second_half \
  > replay/2026-second-half/backups/after-midseason-draft.dump
cp replay/2026-second-half/state/checkpoint.json \
   replay/2026-second-half/backups/checkpoint-after-midseason-draft.json
```

Record this checkpoint's identity in `provenance-manifest.md`, along with
the final `status` output, squad sizes, and any exceptional correction from
section H step 8. This is the retained post-draft checkpoint the acceptance
criteria require, and the recovery baseline for Round 11 onward.

## J. Replay Rounds 11–20 (normal weekly lifecycle)

Rounds 11–20 return to the same normal weekly replay lifecycle as Rounds
1–9 and Round 10 — follow `2026-first-half-replay-playbook.md` section G's
fourteen-step canonical procedure again, unchanged, for each round in turn,
against the second-half installation. This document does not re-list that
procedure; only what is different in the second half is called out below.

Second-half-specific checks:

- **Squads reflect the post-draft state.** Weekly Lineup, Draft Board, and
  Season Centre should show the post-mid-season-draft ownership from
  section H/I, not the pre-draft squads Round 10 used. If a team still
  shows a delisted player as owned, or a drafted player as unowned, stop
  and re-verify the post-draft checkpoint before opening the round.
- **No further Opening Round exceptions apply** — every accepted Opening
  Round rule's compensating round was already resolved by Round 4 in the
  first half (`round-results.md`'s "Opening Round milestone").
- **The draft's `draft_kind="midseason"` row is independent of the
  preseason draft** (`docs/midseason-draft.md`'s "Draft engine reuse"
  section) — Draft Board history for Rounds 11–20 should show both drafts
  distinctly, never merged into one sequence.
- Continue recording replay findings, discrepancies, and audited
  corrections in `docs/evidence/2026-second-half-replay/round-results.md`
  as they arise, in the same style as the first-half `round-results.md`
  and `workflow-findings.md`.
- Back up the database and checkpoint after every round, exactly as in
  section G step 3 (`after-round-N.dump` / `checkpoint-after-round-N.json`
  for N = 11 through 20).
- After each round finalises, re-verify that squads, results, and ladder
  remain mutually coherent (owned players match the post-draft ledger, the
  ladder recomputes cleanly from published results) before opening the
  next round — the same discipline `replay-checkpoint-2026.md` step 16
  already requires for Rounds 1–9.

## K. Finalise Round 20 and checkpoint the home-and-away season

1. Publish all five Round 20 results and verify the completed
   home-and-away ladder (20 rounds, every team's W/D/L/PF/PA/% reconciling)
   exactly as section G step 2 did for Round 10. Record it in
   `round-results.md`'s closing-ladder section.
2. Back up the database and checkpoint:

   ```bash
   $SECOND exec -T database pg_dump -U bbbffl -Fc bbbffl_2026_second_half \
     > replay/2026-second-half/backups/after-round-20.dump
   cp replay/2026-second-half/state/checkpoint.json \
      replay/2026-second-half/backups/checkpoint-after-round-20.json
   ```

3. **This is the retained Round 20/home-and-away checkpoint** the
   acceptance criteria require. Record its identity in
   `provenance-manifest.md` alongside the closing ladder and any
   outstanding historical-stat variance or unresolved question (mirroring
   `round-results.md`'s "Historical-stat variance" section from the first
   half).

## L. Handoff to finals seeding / finals / SuperScore

This playbook stops at the completed home-and-away season. The next phase
(finals seeding from the locked top-five, the four-week finals bracket,
and the four independent SuperScore streams) is a separate, not-yet-written
operational playbook, matching
`docs/evidence/2026-first-half-replay/second-half-handoff.md`'s own
"Intended scope" list (items 4–6) and the roadmap's Milestone E
(`docs/roadmap/2027-season-roadmap.md`). Before starting it:

- take the Round 20 checkpoint from section K as its starting database copy,
  under the same preserve-source/copy-forward discipline this playbook uses
  for the first-half → second-half transition;
- confirm the finals/SuperScore domain implementation (`docs/roadmap/
  2027-season-roadmap.md` milestone E, work packages 35, 37–38) has landed
  before attempting to replay it;
- extend `docs/evidence/2026-second-half-replay/` (or a new
  `2026-finals-replay` evidence directory, following the same convention)
  rather than inventing an unrelated format.

## M. Provenance and evidence — minimum record

At minimum, record the following at each checkpoint named in sections C, D,
G, I, and K (a template lives in
`docs/evidence/2026-second-half-replay/provenance-manifest.md`):

| Field | Where it comes from |
|---|---|
| Source first-half checkpoint/database identifier | Phase 1 closing archive filename + SHA-256 (`phase-one-closeout.md`) |
| File/database path or logical identity | Private backup path, or `bbbffl-2026-second-half` / `bbbffl_2026_second_half` |
| Timestamp of preserved checkpoint | Archive/checkpoint creation time |
| Working-copy identifier | Second-half backup filename + SHA-256 at that boundary |
| Provenance from source to working copy | Explicit `restored_from` pointer (section D step 6) |
| Git commit SHA / tagged application baseline | `git rev-parse HEAD` at the time the checkpoint was taken |
| Migration/schema version | `python -m app.migrations current` output |
| Relevant environment/configuration assumptions | `.env.second-half-replay` values actually used (never the secrets themselves) |
| Round 10 pre-draft checkpoint identity | Section G step 4 |
| Post-mid-season-draft checkpoint identity | Section I |
| Round 20 checkpoint identity | Section K step 3 |
| Evidence of validation performed at each boundary | Ladder/squad/audit verification notes from the relevant section |
| Exceptional audited Scorer correction | Section H step 8, or the equivalent weekly correction from section J |
| Known replay deviation / unresolved historical uncertainty | `round-results.md`'s discrepancy notes |

Sanitised summaries of all of the above may be committed to
`docs/evidence/2026-second-half-replay/`. Database archives, checkpoint
JSON files, backup filenames, private SHA-256 records, credentials, and
page captures are retained privately by the operator and never committed —
identical to the first-half evidence policy in
`docs/evidence/2026-first-half-replay/README.md`.

## N. Recovery guidance

Recovery always prefers restoring/copying from the most recent verified
checkpoint over hand-editing historical data. Where a correction can be
made through an existing audited application workflow, use it instead of
any recovery procedure below.

- **The Round 10 replay needs to be restarted.** Restore the working
  database from the pre-Round-10 backup (section D step 6) and its paired
  checkpoint JSON, then re-run section F from the start. Never restart by
  editing Round 10 rows in place.
- **An error is discovered after Round 10 finalisation but before the
  draft begins.** If it is a genuine historical result/statistic error, use
  the existing audited correction mechanism (`docs/scorer-round-review.md`)
  and let the ladder recalculate — do not hand-edit ladder order. If the
  error is structural (wrong squads, a bad restore), restore from the
  pre-Round-10 backup and redo Round 10. Either way, retake the Round
  10/pre-draft checkpoint (section G) before continuing — the previous one
  is now stale.
- **A mid-season draft reconstruction needs to be corrected or restarted.**
  For a single wrong action still within the draft's own lifecycle, prefer
  the matching audited in-workflow correction: `reverse-trade` for an
  approved trade (only before `lock-delistings`), `override-order` for the
  draft order, or `msd correct-selection`/`reopen-draft` (section H step 7 —
  not the ordinary `docs/scorer-draft-workflow.md` correction, which targets
  the wrong draft once both a preseason and mid-season draft exist) for a
  wrong pick. For a deeper reconstruction error (wrong delistings discovered
  after `lock-delistings`, or a corrupted draft state), restore the working
  database from the Round 10/pre-draft checkpoint (section G step 3) and
  redo section H from `set-trigger-round`/`confirm-ladder` onward.
- **A later Round 11–20 replay step damages or invalidates the working
  database.** Restore from the most recent verified round backup
  (`after-round-N.dump` + its paired checkpoint JSON) and redo only the
  rounds after N. Never patch forward from a damaged state.
- **The current working database needs to be abandoned and recreated from
  the most recent verified checkpoint.** Stop the second-half installation,
  confirm which backup is the most recent one already recorded in
  `provenance-manifest.md` as validated (not merely taken) — e.g.
  `after-round-15.dump` and its paired `checkpoint-after-round-15.json`.
  Restore that database backup using section D step 2's `pg_restore`/`psql`
  commands (`pg_restore --clean --if-exists` drops and recreates conflicting
  objects, so this always restores into the existing
  `bbbffl_2026_second_half` database, never by deleting and recreating the
  `second-half-database` volume) **and** copy that backup's own paired
  checkpoint JSON over `replay/2026-second-half/state/checkpoint.json` — do
  not re-run section D step 3's fresh-checkpoint initialisation here, and
  never leave the database and the checkpoint file at different rounds'
  state: a database restored to after Round 15 paired with, say, the
  original pre-Round-10 checkpoint would present a lifecycle/lockout state
  from Round 15 alongside a replay clock and finalised-round set from
  before Round 10. As at every step, the original first-half checkpoint
  (section C) is never touched by this recovery.

## O. Hard Round 20 exit gate

Do not declare the second-half home-and-away replay successful until all
are checked:

- [ ] The first-half source checkpoint remains preserved, unstarted, and
      unmutated throughout (section C).
- [ ] The second-half working copy's provenance back to that source
      checkpoint is recorded (section D step 6, `provenance-manifest.md`).
- [ ] Round 10 is `final` using the pre-draft squads, with its ladder
      verified and a retained pre-draft checkpoint (sections F–G).
- [ ] The mid-season draft reached `draft_complete`/`complete` with correct
      final squad sizes, post-draft trading explicitly closed, and a
      retained post-draft checkpoint (sections H–I).
- [ ] Rounds 11–20 are all `final` with ten authoritative submissions each,
      squads/results/ladder verified coherent after every round (section J).
- [ ] Round 20's home-and-away ladder is verified and a retained Round 20
      checkpoint exists (section K).
- [ ] Every exceptional Scorer correction across the phase is recorded with
      actor/reason/audit reference.
- [ ] Replay findings, discrepancies, and unresolved historical
      uncertainties are recorded in
      `docs/evidence/2026-second-half-replay/round-results.md`.
- [ ] No unexpected draft is open and no unexpected lineup draft/submission
      remains.
- [ ] The same season/database is ready to continue into finals seeding
      without rebuilding the competition (section L).
