# 2026 finals and SuperScore replay playbook (issue #194)

## A. Purpose and scope

This is the operational playbook `docs/2026-second-half-replay-playbook.md`
section L has pointed to as "not-yet-written" since PR #188, and the one
`docs/2026-finals-superscore-design.md`'s "Backup/recovery" note anticipates.
It picks up exactly where
[`2026-second-half-replay-playbook.md`](2026-second-half-replay-playbook.md)
leaves off -- the completed Round 20 home-and-away season, plus the audited
finals-seeding snapshot (section L, issue #187) -- and covers the main
finals bracket (weeks 1-4), the four independent SuperScore rounds
(SS1-SS4), issue #195's end-of-season completion transaction, and issue
#194's own final archival checkpoint.

**Read `docs/2026-finals-superscore-design.md`'s "Audit, correction and
recovery" section (especially "Checkpoint timing") before running any step
below.** This playbook operationalises that design; it does not restate its
reasoning. Every command below names a real, merged module/CLI/route --
never a hypothetical one -- verified directly against the repository at the
time this playbook was written (migration head `0033_season_award`).

**This document does not itself replay real 2026 finals/SuperScore history.**
`docs/2026-finals-superscore-design.md`'s "Historical gaps requiring Steve's
confirmation" (items 1-2) records that no recovered historical evidence for
the actual 2026 finals-week or SuperScore SS1-SS4 lineups/results has been
supplied to this repository. Unless/until such evidence is supplied, running
this playbook is necessarily a fresh (evidence-free) simulation forward from
the confirmed seed, not a reconstruction of real 2026 results -- record
which applies in `docs/evidence/2026-finals-replay/provenance-manifest.md`
before treating any of its output as historical fact.

## B. Operator surface reference

Every command below runs the same way the second-half playbook's own
commands do: through the replay Docker Compose installation, with
`--database-url` mounted first, migrations run automatically by mutating
commands, and every mutating command refusing to run while
`BBBFFL_ENVIRONMENT=production`. Substitute your own `$FINALS` compose
function (mirroring section D's `$SECOND`) and the working database's own
`--database-url`.

| Domain | Surface | Module |
|---|---|---|
| Finals-seeding snapshot | CLI: `scripts.finals_seeding_2026` (`preview`, `apply`) | `app.finals_seeding` (issue #187) |
| Finals bracket creation/lifecycle | CLI: `scripts.finals_bracket_2026` (`create-bracket preview\|apply`, `open-week`, `advance preview\|apply`, `rewind`) | `app.finals`, `app.finals_preflight` (issue #190) |
| Finals result publish/correct | HTTP: `/api/admin/finals/{bracket_id}/weeks/{week_number}/publish`, `/api/admin/finals/{bracket_id}/matchups/{matchup_id}/correct` | `app.finals_review` (issue #191) |
| Finals DNP/Interchange/override rulings | HTTP: `/api/admin/round-review/{round_id}/dnp`\|`/interchange`\|`/override` (the same matchup-keyed routes ordinary rounds use -- reusable unchanged because issue #197 chose Path 1) | `app.round_review` |
| Coach finals lineup submission | HTTP: `/coach/seasons/{season_id}/rounds/{round_id}/lineup` (the same generic route ordinary rounds use) | `app.lineups`, `app.coach_lineup` |
| SuperScore stream/round setup | CLI: `scripts.superscore_round_2026` (`ensure-stream`, `ensure-round`, `confirm-mapping`, `setup-round`, `open-round`, `status`) -- **added by issue #194**, see below | `app.superscore_round` (issue #192) |
| SuperScore entry-scoped rulings | CLI: `scripts.superscore_review_2026` (`dnp`, `interchange`, `override`, `status`) -- **added by issue #194**, see below | `app.superscore_review` (issue #192) |
| SuperScore leaderboard publish/correct | HTTP: `/api/season-superscore/scorer/rounds/{round_id}/calculate`, `/publish` | `app.superscore_results` (issue #193) |
| Coach SuperScore lineup submission | HTTP: `/coach/seasons/{season_id}/rounds/{round_id}/lineup` (identical route; SuperScore rounds carry no matchup, only a round) | `app.lineups`, `app.coach_lineup` |
| Season completion (steps 1-6) | CLI: `scripts.season_completion_2026` (`preview`, `complete`) -- **added by issue #194**, see below | `app.season_completion` (issue #195) |
| Final archival checkpoint guard (step 7) | CLI: `scripts.season_archival_checkpoint_2026` (`verify`) -- **added by issue #194** | `app.season_archival` (issue #194) |

**Issue #194 found that the SuperScore round-setup/review and
season-completion domains had no operator-reachable entry point at all** --
fully implemented and tested, but callable only from test code
(`docs/evidence/2026-finals-replay/workflow-findings.md`'s finding 1). The
three CLI scripts marked "added by issue #194" above close that gap,
mirroring `scripts/finals_bracket_2026.py`/`scripts/finals_seeding_2026.py`'s
established preview/apply, `--reason`-required, production-guarded shape,
and change no domain logic beyond one PostgreSQL correctness fix (finding 2,
same document) needed to make the SuperScore round-setup CLI actually work
against real PostgreSQL.

## C. Post-finals-seeding-apply recovery point (do this first, independent of everything else)

**This step is explicitly independent of #190-#195 (issue #194's own
scope note) -- take it as soon as this playbook starts, even before the
finals bracket exists.** Verify it has not already been taken before
relying on it: `docs/evidence/2026-second-half-replay/provenance-manifest.md`'s
"Round 20 / home-and-away boundary" row records only the backup taken
**before** `scripts.finals_seeding_2026 ... apply` -- confirmed by direct
inspection, not assumed. Do not skip this step believing an earlier
document draft's now-corrected claim that a post-apply backup already
exists (`docs/2026-finals-superscore-design.md`'s "Checkpoint timing",
Codex review PR #196 twenty-first round).

1. Confirm the finals-seeding snapshot from section L of the second-half
   playbook has already been applied:

   ```bash
   $FINALS run --rm -v "$PWD/bbbffl_app:/app" app \
     python -m scripts.finals_seeding_2026 --database-url <url> \
     preview --season-id <season_id> --competition-id <ordinary_competition_id>
   ```

   Expect `replay_context_ready: true`, `apply_permitted: false` (already
   applied), and the historical seed order matching
   `docs/evidence/2026-second-half-replay/provenance-manifest.md`'s
   "Finals-seeding snapshot" row. If instead `apply_permitted: true`, the
   snapshot has not been applied yet -- stop and run section L of the
   second-half playbook first.

2. **Take the paired database/checkpoint backup, exactly like every other
   boundary in this replay:**

   ```bash
   $FINALS exec -T database pg_dump -U bbbffl -Fc bbbffl_2026_finals \
     > replay/2026-finals/backups/post-finals-seeding-apply.dump
   cp replay/2026-finals/state/checkpoint.json \
      replay/2026-finals/backups/checkpoint-post-finals-seeding-apply.json
   ```

3. **Verify the archive is readable** before trusting it as a recovery
   point:

   ```bash
   pg_restore --list replay/2026-finals/backups/post-finals-seeding-apply.dump
   ```

4. **Record this boundary in
   [`docs/evidence/2026-finals-replay/provenance-manifest.md`](evidence/2026-finals-replay/provenance-manifest.md)'s
   "Post-finals-seeding-apply boundary" section** -- filename (private),
   SHA-256 (private), application commit, migration head, and the
   `pg_restore --list` PASS -- before any finals bracket creation or
   SuperScore stream setup relies on this as a recovery point.

## D. Create the finals bracket and run weeks 1-4

1. **Create the bracket** (refuses unless the finals-seeding snapshot from
   section C exists, per `scripts/finals_bracket_2026.py`'s own repo-owner
   decision -- see that script's docstring):

   ```bash
   $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.finals_bracket_2026 \
     --database-url <url> create-bracket preview --season-id <season_id> \
     --competition-id <finals_competition_id> --ordinary-competition-id <ordinary_competition_id>
   $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.finals_bracket_2026 \
     --database-url <url> create-bracket apply --season-id <season_id> \
     --competition-id <finals_competition_id> --ordinary-competition-id <ordinary_competition_id> \
     --reason "2026 finals replay: bracket creation per issue #190"
   ```

   Record `bracket_id` and the printed seed order in
   `provenance-manifest.md`.

2. **For each week in turn (1 through 4):**

   a. Confirm the week's AFL-round mapping is accepted (the normal
      `app.round_mapping` flow, via the finals preflight surface or
      `app/routes/round_preflight.py`'s mapping-acceptance route, exactly
      as an ordinary round's mapping is confirmed).

   b. **Open the week:**

      ```bash
      $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.finals_bracket_2026 \
        --database-url <url> open-week --bracket-id <bracket_id> --week <N> \
        --reason "2026 finals replay: open week <N>"
      ```

   c. Run lineup submission, lockout, calculation, and Scorer DNP/
      Interchange/override rulings exactly as an ordinary round -- the
      coach lineup route and the matchup-keyed round-review routes are
      reused unchanged (see section B's table).

   d. **Publish the week's result(s)** (a variable match count -- two in
      weeks 1-2, one in weeks 3-4; week 1's bye publishes nothing for seed
      1):

      ```
      POST /api/admin/finals/{bracket_id}/weeks/{N}/publish?reason=...
      ```

   e. **Advance the bracket** to derive weeks 2-4's pairing (skip for week
      4, which has no downstream week):

      ```bash
      $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.finals_bracket_2026 \
        --database-url <url> advance preview --bracket-id <bracket_id> --from-week <N>
      $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.finals_bracket_2026 \
        --database-url <url> advance apply --bracket-id <bracket_id> --from-week <N> \
        --reason "2026 finals replay: advance from week <N>" \
        --expected-versions '<paste from the preview output above>'
      ```

   f. **Checkpoint this week's boundary** (the same paired backup pattern
      as every other checkpoint in this replay):

      ```bash
      $FINALS exec -T database pg_dump -U bbbffl -Fc bbbffl_2026_finals \
        > replay/2026-finals/backups/after-finals-week-<N>.dump
      cp replay/2026-finals/state/checkpoint.json \
         replay/2026-finals/backups/checkpoint-after-finals-week-<N>.json
      ```

      Record it in `provenance-manifest.md`'s "Finals week checkpoints"
      table before opening the next week.

3. Week 4's publish (the Grand Final) also records `finals.premier.
   recorded`/`finals.wooden_spoon.recorded` (see `docs/audit-events.md`'s
   catalogue addendum for why these are **not** the official season
   awards) -- record both event ids in `provenance-manifest.md`.

## E. Run the four SuperScore rounds (SS1-SS4)

SS1-SS4 run across the *same* four AFL rounds as finals weeks 1-4
(`docs/2026-finals-superscore-design.md`'s confirmed rule) and can be set up
concurrently with the finals bracket above, in any order relative to it,
since neither stream depends on the other's lifecycle.

1. **Create the SuperScore stream once** (idempotent):

   ```bash
   $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.superscore_round_2026 \
     --database-url <url> ensure-stream --season-id <season_id> \
     --rules-version-id <rules_version_id> --ordinary-competition-id <ordinary_competition_id> \
     --reason "2026 finals replay: SuperScore stream setup per issue #192"
   ```

2. **For each of SS1-SS4 in turn:**

   a. **Create the round's logical row:**

      ```bash
      $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.superscore_round_2026 \
        --database-url <url> ensure-round --competition-id <superscore_competition_id> --round-number <N>
      ```

   b. **Confirm its AFL-round mapping**, sourced from the *corresponding
      finals week's own accepted mapping* (never re-derived independently
      -- see `scripts/superscore_round_2026.py`'s own docstring):

      ```bash
      $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.superscore_round_2026 \
        --database-url <url> confirm-mapping --round-id <ss_round_id> \
        --afl-season-id <year> --afl-round-id <afl_round_id_from_finals_week_N> \
        --evidence-path /replay/evidence/2026-second-half.json \
        --checkpoint-path /replay/state/checkpoint.json \
        --reason "2026 finals replay: SS<N> mapping, concurrent with finals week <N>"
      ```

      **`--checkpoint-path` is required here, not optional** (Codex
      review): `2026-second-half.json`'s manifest declares
      `lifecycle_semantics: "scheduled-start-plus-final-results-checkpoint"`
      (`app.replay_acquisition`), and `ReplayAflDataSource` fails closed
      (`ReplayEvidenceError`) loading any such package without an explicit
      persisted replay checkpoint (`app/replay.py`'s `_load`). Every
      `confirm-mapping` invocation in this section needs it.

   c. **Set up the round** (creates the lifecycle row and the complete
      ten-entry review-state row set atomically):

      ```bash
      $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.superscore_round_2026 \
        --database-url <url> setup-round --round-id <ss_round_id> \
        --reason "2026 finals replay: SS<N> round setup"
      ```

   d. **Open the round** (refuses unless setup succeeded):

      ```bash
      $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.superscore_round_2026 \
        --database-url <url> open-round --round-id <ss_round_id> \
        --reason "2026 finals replay: SS<N> open"
      ```

   e. Run lineup submission (via the coach lineup route -- all ten entries
      participate in every SuperScore round, including eliminated finals
      seeds) and lockout exactly as an ordinary round.

   f. Record any DNP/Interchange/override ruling for an entry with the
      **entry-scoped** CLI (not the matchup-keyed round-review routes,
      which do not apply -- SuperScore has no matchups):

      ```bash
      $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.superscore_review_2026 \
        --database-url <url> status --round-id <ss_round_id> --season-entry-id <entry_id>
      $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.superscore_review_2026 \
        --database-url <url> dnp --round-id <ss_round_id> --season-entry-id <entry_id> \
        --slot F1 --dnp true --expected-review-version <from status above> \
        --reason "2026 finals replay: SS<N> DNP ruling"
      ```

      Always run `status` first and pass its `review_version` as
      `--expected-review-version` -- a stale value is rejected rather than
      silently applied.

   g. **Calculate and publish the leaderboard:**

      ```
      POST /api/season-superscore/scorer/rounds/{ss_round_id}/calculate
      POST /api/season-superscore/scorer/rounds/{ss_round_id}/publish {"reason": "..."}
      ```

      The same `publish` call also handles correction on a later
      re-publish (versioned, append-only -- see `docs/audit-events.md`).

   h. **Checkpoint this round's boundary:**

      ```bash
      $FINALS exec -T database pg_dump -U bbbffl -Fc bbbffl_2026_finals \
        > replay/2026-finals/backups/after-superscore-ss<N>.dump
      cp replay/2026-finals/state/checkpoint.json \
         replay/2026-finals/backups/checkpoint-after-superscore-ss<N>.json
      ```

      Record it in `provenance-manifest.md`'s "SuperScore stream/round
      checkpoints" table before opening the next round.

## F. Finals corrections and bracket rewind

See section I ("Recovery guidance") below for the full decision tree. In
outline, once a finals result is published:

- A **result correction with no downstream play state yet** (the common
  case, discovered promptly): `POST /api/admin/finals/{bracket_id}/matchups/{matchup_id}/correct`
  automatically re-derives and supersedes the immediately downstream
  pairing/elimination together, atomically
  (`FinalsBracketRepository.rewind_bracket_in_transaction`, invoked
  internally by `correct_finals_result`).
- A **rewind requested independently of a fresh correction** (e.g.
  re-deriving after discovering the automatic cascade above was blocked)
  uses the CLI directly:

  ```bash
  $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.finals_bracket_2026 \
    --database-url <url> rewind --bracket-id <bracket_id> --from-week <N> \
    --reason "..." # preview first (no --apply), then --apply once satisfied
  ```

- If the downstream week already has **any** competitive play state (an
  authoritative lineup submission, a genuinely locked position, a ruling/
  adjudication/override, a persisted calculation, or a published official
  result), both paths above fail closed
  (`DownstreamPlayStateError`/`report`) and require explicit human
  competition intervention -- see section J's policy carry-forward and
  section I below.

## G. Observe issue #195's completion, then take the final archival checkpoint (step 7)

**This is the one step #194 must get exactly right: never before, never
racing, always bound to the exact identifiers #195's transaction
established.** #195 owns steps 1-6 of "Checkpoint timing"; #194 owns only
step 7, strictly afterward.

1. **Confirm readiness** (every required finals week and all four
   SuperScore rounds `final`):

   ```bash
   $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.season_completion_2026 \
     --database-url <url> preview --season-id <season_id>
   ```

   Do not proceed until `ready: true`. `ready: false` reports exactly
   which round(s) are not yet `final` -- the Grand Final and SS4 being
   published is explicitly **not** sufficient evidence by itself (per the
   design's fail-closed readiness gate); every required round is checked
   individually.

2. **Run the completion transaction** (issue #195's atomic steps 1-6: lock
   the season row; verify readiness; materialise/supersede the
   premiership and wooden-spoon awards against locked, effective
   provenance; record the `season.completed` event; transition to
   `completed`; commit):

   ```bash
   $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.season_completion_2026 \
     --database-url <url> complete --season-id <season_id> \
     --reason "2026 finals/SuperScore replay: season completion per issue #195"
   ```

   Record the printed `completed_season_version`/`completion_event_id`
   and both award ids in `provenance-manifest.md`'s "Season completion"
   section immediately -- this is the exact identifier pair step 3 below
   must independently reproduce, not merely echo.

3. **Independently verify completion before taking any archival evidence**
   -- this is the proof required by the design's step-7 boundary and by
   this issue's acceptance criterion F, and it is genuinely code-backed
   (`app/season_archival.py`, tested in `tests/test_season_archival.py`),
   not merely a documented convention:

   ```bash
   $FINALS run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.season_archival_checkpoint_2026 \
     --database-url <url> verify --season-id <season_id> \
     --expected-completion-event-id <completion_event_id from step 2>
   ```

   `verify` is strictly read-only: it never locks, never migrates, never
   writes. It fails closed (`SeasonNotCompletedError`) unless
   `bbbffl_season.lifecycle_state == 'completed'` -- a state that, once
   observed, cannot describe a version read before or racing step 2's
   commit, because `SeasonRepository.guard_writable` refuses every
   further result-changing write once `completed` is observed, and no
   reopen pathway exists in this codebase. It additionally requires
   exactly one `season.completed` audit event to exist for this season
   (never trusting the bare lifecycle label alone), and, when
   `--expected-completion-event-id` is supplied, fails closed
   (`CompletionEventMismatchError`) if the currently observed completion
   event does not match the one step 2 printed -- catching the case where
   archival evidence would otherwise silently bind itself to a *different*
   completion than the one the operator reviewed.

4. **Only after `verify` exits 0**, take the final paired backup:

   ```bash
   $FINALS exec -T database pg_dump -U bbbffl -Fc bbbffl_2026_finals \
     > replay/2026-finals/backups/final-archival-checkpoint.dump
   cp replay/2026-finals/state/checkpoint.json \
      replay/2026-finals/backups/checkpoint-final-archival.json
   pg_restore --list replay/2026-finals/backups/final-archival-checkpoint.dump
   ```

5. **Record the complete final archival checkpoint entry** in
   `provenance-manifest.md`, with `completed_season_version`/
   `completion_event_id` copied from step 3's `verify` output (not step
   2's `complete` output -- they must match, but `verify`'s independent
   re-derivation is the one that actually proves the binding), plus the
   backup filenames/SHA-256/readability check.

This is the terminal recovery point for the completed 2026 season.

## H. Explicit checkpoint boundaries (summary)

At minimum, this playbook takes a paired `pg_dump` + checkpoint JSON at:

- **Post-finals-seeding-apply** (section C) -- before any finals/SuperScore
  work relies on it as a recovery point.
- **After each finals week finalises** (section D.2.f) -- four boundaries.
- **After each SuperScore round finalises** (section E.2.h) -- four
  boundaries.
- **The final end-of-season archival checkpoint** (section G.4) -- taken
  **only** after `scripts.season_archival_checkpoint_2026 verify` passes,
  never before, never on a whim between finals/SuperScore boundaries.

**The final archival checkpoint is categorically different from every
checkpoint before it.** Every earlier checkpoint is an ordinary interim
recovery point -- restore from it and continue the replay forward. The
final archival checkpoint is the terminal, step-7-only record of a
`completed` season bound to a specific `completion_event_id`; it is never
retroactively supersedable while the season stays `completed` (no reopen
pathway exists), and it is the one checkpoint that must never be taken
before its gating condition (`verify` passing) holds.

## I. Recovery guidance

Recovery always prefers restoring/copying from the most recent verified
checkpoint over hand-editing historical data. Where an existing audited
correction workflow applies, prefer it over checkpoint restore -- exactly
`docs/2026-second-half-replay-playbook.md` section N's principle, applied
to this phase's own failure modes.

- **A finals result needs correction before any downstream competitive
  play state exists.** Use `POST /api/admin/finals/{bracket_id}/matchups/
  {matchup_id}/correct` (section F). This is the normal, audited path --
  it preserves the original result version and automatically re-derives
  the downstream pairing/elimination in the same operation. Never restore
  a checkpoint for this; a correction is strictly more precise and leaves
  a complete audit trail of both the original and corrected result.
- **A finals correction changes the winner/loser and the downstream week
  has no competitive play state yet.** The correction above's automatic
  rewind handles this case directly -- confirm the printed re-derived
  pairing/elimination against the correction, and re-verify any
  now-superseded downstream checkpoint understanding before continuing.
- **A finals correction is needed but the downstream week already has
  competitive play state** (an authoritative lineup submission, a
  genuinely locked position, a ruling/adjudication/override, a persisted
  calculation, or a published official result). The software fails closed
  (`DownstreamPlayStateError`) by design -- this is not a bug to work
  around with a checkpoint restore. See section J: this is the exact
  point where the technical safety boundary hands off to a human
  competition-governance decision. Do not restore a checkpoint to force
  the automatic cascade through; that would silently discard the
  downstream week's genuine competitive history. Escalate for an explicit
  human ruling on what competitive outcome should apply, then apply it
  through whatever audited mechanism that ruling requires (which may
  itself be a documented, reasoned exception outside this playbook's
  scope).
- **A bracket-advance step needs to be redone** (e.g. run against a
  wrong/incomplete official result by operator error, with no downstream
  play state yet). Prefer restoring the most recent finals-week checkpoint
  taken before the advance (section D.2.f) and redoing `advance` correctly
  from there, rather than attempting to hand-edit the persisted pairing.
- **A SuperScore published leaderboard requires correction.** Re-run
  `POST /api/season-superscore/scorer/rounds/{round_id}/publish` -- the
  same command handles both the first publish and a later correction
  (versioned, append-only; see `docs/audit-events.md`'s
  `superscore.leaderboard.corrected`). Never restore a checkpoint for an
  ordinary correction; this path is strictly more precise.
- **The replay process fails between logical boundaries** (e.g. a
  `setup-round` or `open-round` call is interrupted). Every mutating
  command in this playbook is either idempotent against its own prior
  success (`ensure-stream`, `ensure-round`, `confirm-mapping`,
  `setup-round`, `create-bracket apply`) or fails atomically with nothing
  partially committed (issue #195's completion transaction; issue #192's
  review-state row set). Re-run the same command; do not restore a
  checkpoint for an interruption alone unless the re-run itself reports an
  unexpected state.
- **Completion (section G step 2) has occurred but the archival checkpoint
  (section G steps 3-5) has not yet been taken.** This is a normal,
  expected intermediate state, not a failure -- the season is `completed`
  and durably write-fenced regardless of when the archival evidence is
  taken. Simply continue from section G step 3 (`verify`); there is
  nothing to recover, since nothing about the completed state is at risk
  of being lost (no reopen pathway exists to threaten it).
- **The archival metadata does not match the exact `completion_event_id`/
  `completed_season_version` #195 established** (e.g. a mistyped value was
  recorded in `provenance-manifest.md`, or `verify` was run with the wrong
  `--expected-completion-event-id` and failed). Never hand-edit the
  provenance-manifest entry to make it agree without re-running `verify`.
  Re-run `scripts.season_archival_checkpoint_2026 verify --season-id
  <season_id>` with **no** `--expected-completion-event-id` to freshly
  re-derive the authoritative identifiers from the database itself, then
  correct the manifest entry to match that fresh output exactly. If the
  freshly re-derived identifiers differ from what was recorded, treat that
  as a serious finding requiring investigation before proceeding (it
  should be unreachable given no reopen pathway exists) -- do not silently
  overwrite one recorded value with another without understanding why they
  differed.

## J. Finals policy: tie-break and correction/rewind (confirmed, issue #190/#191)

Carried forward verbatim from the confirmed policy issue #194's own comment
thread required (`docs/2026-finals-superscore-design.md`'s "Historical
gaps requiring Steve's confirmation", items 3-4, and `docs/finals-bracket.md`).

### Technical safety boundary (what the software enforces)

1. **Tie-break.** A tied finals match, including a tied Grand Final, is won
   for progression purposes by the team with the higher **frozen
   end-of-home-and-away finals seed** -- the seed captured when the
   bracket was created, never a live/recomputed ladder, percentage, PF, or
   any other later ordering. Implemented in
   `app.finals.FinalsBracketRepository._winner_loser`; every seed
   comparison reads the bracket's own frozen `finals_bracket_seed` rows.
2. **Correction preserves history.** A corrected finals result never
   destructively overwrites the pairing/elimination history it already
   produced. The original result, pairing and elimination revisions are
   preserved, with actor/reason provenance recorded for every superseding
   action (`docs/audit-events.md`'s append-only convention, applied
   without exception here).
3. **Automated rewind, bounded.** A corrected result that changes the
   winner/loser may trigger an audited bracket rewind/re-derivation of the
   *immediately* downstream pairing and its elimination record, together,
   atomically -- but **only** while that downstream week has no
   competitive play state.
4. **Fail closed once downstream play state exists.** "Downstream play
   state" means, at minimum: an authoritative lineup submission, a
   genuinely locked position, a ruling/adjudication/override, a persisted
   calculation, or a published official result. If any of these exists for
   the downstream week, automatic cascading fails closed
   (`DownstreamPlayStateError`, reporting the affected artifacts) and
   requires explicit human competition intervention. It never
   automatically invalidates or replays existing downstream state, and it
   never recurses past the immediately downstream week (a correction two
   weeks upstream of an existing pairing is always blocked by that
   pairing's own downstream week already necessarily having competitive
   state, per the causal ordering `advance_bracket` enforces).

**The distinction that matters:** points 1-4 above are the *technical*
safety boundary the software enforces automatically and without exception.
They are not, and must not be read as, a decision about what competitive
outcome *should* apply once a downstream finals week has materially begun.
That is a separate, later **human league/governance decision** -- the
software's job ends at failing closed and surfacing the affected artifacts
for that decision; it never makes the decision itself.

### Plain-language source section (for the coach handbook / rules review)

This section is written to stand on its own, for later use verbatim or
near-verbatim in a human coach handbook or rules-review document, so the
operational implementation above and the coach-facing rule never drift
apart:

> **Tied finals matches.** If a finals match (including the Grand Final)
> finishes level on the field, the team that finished higher in the
> official end-of-home-and-away finals order advances (or wins the
> premiership, for a tied Grand Final). This order is locked in once the
> finals bracket is drawn and does not change during the finals series,
> even if it would have looked different recalculated later.
>
> **Correcting a finals result.** If a finals result needs to be corrected
> after the fact (for example, a scoring error discovered later), the
> correction is recorded and the original result is kept on record, not
> deleted -- both are visible to anyone who needs to see the history.
>
> **What happens to the next week if a correction changes who won.** If
> the next week's draw was built from the result being corrected, and
> nothing has happened yet for that next week (no team has picked a side,
> no game has been locked in, no scores have been calculated or published
> for it), the draw for that next week is automatically redrawn to match
> the corrected result. If anything *has* already happened for that next
> week, the system will **not** automatically redraw it -- instead, it
> stops and flags the situation for the competition organisers to decide
> what's fair, since by that point real teams have already made real
> decisions based on the original draw.

## K. Hard exit-gate checklist

Do not declare the finals/SuperScore replay phase complete until all are
checked:

- [ ] The post-finals-seeding-apply paired backup is taken and recorded
      (section C), **not** relied on as pre-existing without verification.
- [ ] All four finals weeks are `final`, with every result published and
      any correction's audit trail (including any rewind) recorded
      (section D/F).
- [ ] All four SuperScore rounds (SS1-SS4) are `final`, with every
      leaderboard published and any correction recorded (section E).
- [ ] Every audited correction (finals result, bracket rewind, SuperScore
      leaderboard correction, DNP/Interchange/override ruling) across the
      phase is reconciled and recorded with actor/reason/audit-event
      provenance -- no known open discrepancy is left silent.
- [ ] `scripts.season_completion_2026 preview` reported `ready: true`
      **before** `complete` was run (section G step 1) -- not inferred
      from the Grand Final/SS4 alone.
- [ ] `scripts.season_completion_2026 complete` succeeded; the season is
      confirmed `completed` (section G step 2).
- [ ] `scripts.season_archival_checkpoint_2026 verify` succeeded
      **against the exact `completion_event_id` `complete` printed**
      (section G step 3) -- not a different or unverified identifier.
- [ ] The final archival checkpoint (paired backup + checkpoint JSON) was
      taken **only after** `verify` succeeded, never before or
      concurrently (section G step 4).
- [ ] `docs/evidence/2026-finals-replay/provenance-manifest.md` is fully
      filled in for every boundary above -- no `<...>` placeholder remains
      for a boundary claimed complete.
- [ ] Required workflow/UX findings from actually running this phase are
      captured in `docs/evidence/2026-finals-replay/round-results.md` and
      `ux-findings.md` (created at that point, in the shape
      `docs/evidence/2026-second-half-replay/` establishes).

**Grand Final published + SS4 published is never, by itself, sufficient**
to check any of the above -- this mirrors
`docs/2026-second-half-replay-playbook.md` section O's equivalent
discipline for Round 20, applied to this phase's own terminal rounds.

## L. Provenance record

See
[`docs/evidence/2026-finals-replay/provenance-manifest.md`](evidence/2026-finals-replay/provenance-manifest.md)
for the full template and current record -- structured identically in
spirit to `docs/2026-second-half-replay-playbook.md` section M's table,
adapted to this phase's own boundaries (post-apply, per-finals-week,
per-SuperScore-round, final archival).
