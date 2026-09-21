# 2027 live-season readiness

**Status:** current-state readiness assessment, 21 September 2026.
**Supersedes:** the current-state claims in
[`docs/roadmap/2027-season-roadmap.md`](roadmap/2027-season-roadmap.md),
which is now a historical planning baseline from 23 August 2026 -- see that
document's own status note. This document does not replace the roadmap's
sequencing/decision record, only its capability claims.
**Evidence base:** the completed 2026 full-season replay
([`docs/evidence/2026-full-season-replay-summary.md`](evidence/2026-full-season-replay-summary.md)
and the three phase evidence directories it synthesises), the targeted
current-code regression run under issue #224 (Stages A-D, below), and the
repository's automated test suite.

This is the authoritative current-state document for whether BBBFFL can run
a live 2027 season. Where it and any older document disagree, this
document is correct as of its status date above.

## Release criterion

Per issue #224:

> **v0.1** means the season can be run end-to-end by normal users without
> routine CLI, database or UUID knowledge. **v0.2** is convenience/polish
> once that condition is satisfied.

"Normal users" means coaches, the Scorer and the Administrator operating
through the browser. It does not include an operator's own database
backup/restore tooling, which is inherently an administrative/CLI concern
in any web application and is judged separately (see "Production/operations
readiness" below), nor does it include the 2026-replay-only tooling
described in the next section, which is explicitly out of scope for live
2027 operation.

## Readiness categories

| Category | Meaning |
|---|---|
| **Replay-proven** | Exercised end-to-end against real persisted state during the 2026 historical replay, at that phase's own historical application/migration baseline. |
| **Current-code regression-proven** | Re-exercised against real persisted state on current `main`, from a disposable restored checkpoint copy, under issue #224's targeted regression (Stages A-D) or the separate mid-season-draft acceptance pass. |
| **Automated-test-proven** | Covered by the repository's `pytest` suite (unit/integration/concurrency/migration tests) but not exercised end-to-end by a human operator driving the browser or CLI against persisted state. |
| **Staging/rehearsal-needed** | Implemented and at least automated-test-proven, but not yet rehearsed under conditions representative of live production use (a real deployment, a non-technical operator, or both). |
| **Outstanding / blocks v0.1** | Missing, or requires routine CLI/database/UUID knowledge for a workflow a normal user must perform in an ordinary season. |
| **Deferred / v0.2 candidate** | Not required to run a normal season end-to-end; convenience or polish. |

A domain frequently spans more than one category (for example: the
underlying rules replay-proven, a specific presentation detail deferred to
v0.2). The tables below classify at the capability level, not only the
domain level, for exactly that reason.

## Replay checkpoint manipulation is not a live-2027 concept

Every phase of the 2026 replay used replay-only infrastructure that has no
live-2027 counterpart and must not be confused with it:

- `app.replay.ReplayAflDataSource`/`ReplayClock` and the
  `BBBFFL_AFL_MODE=replay` / `BBBFFL_AFL_REPLAY_EVIDENCE_PATH` configuration
  (`docs/replay-harness.md`) -- live 2027 always uses the real `afl-api`
  client (`BBBFFL_AFL_MODE=live`, the default outside replay/test);
- manually advancing the replay checkpoint's stage/time to reveal
  historical AFL evidence, and the manual restarts that follow it
  (`2026-finals-replay/workflow-findings.md` finding 8; the current-code
  regression's Stage C/D notes below) -- a live match's status and stats
  simply arrive from the real provider as the match happens;
- the 2026-scoped finals-seeding snapshot tool
  (`app.finals_seeding`, `scripts/finals_seeding_2026.py`) -- structurally
  restricted to the 2026 replay season and inapplicable to any other
  season, live 2027 included (see
  `docs/evidence/2026-second-half-replay/finals-seeding-2026.md`);
  no live season ever has a "historical Scorer-error" seeding correction to
  apply;
- the disposable restore/migrate-forward pattern (`bbbffl-v01-regression`,
  `bbbffl-midseason-draft`, etc.) used to prove current-code compatibility
  against preserved 2026 state -- a live 2027 database has no earlier
  season's checkpoint to restore from.

None of this is part of what a Coach, Scorer or Administrator does during a
live season, and none of it counts toward -- or against -- the v0.1
criterion above.

## Current-code regression evidence (Stages A-D)

Issue #224 required a targeted regression proving representative
first-half scenarios still work on current code, not a second full replay.
This is now complete, run against current `main` at
`37fec7156e24d73b62e0465fac9f4f4c5ebf2c5d` (merge of #232), with each
restored historical database migrated to the current migration head before
use, on a disposable `bbbffl-v01-regression` stack. No preserved source
replay database or checkpoint was mutated.

| Stage | Source checkpoint | Scope | Result |
|---|---|---|---|
| A | Pre-pick-1 preseason restore point, migrated from `0022_acting_context` | Coach-facing pre-season draft access (#229/#230), Scorer/Admin proxy picks (#231), private shortlists, immediate unavailability, fail-closed turn ownership | PASS |
| B | `draft-after-pick-200.sql` | Picks 201-220 (mixed Coach/proxy), shortlist updates, draft navigation, finalisation, ten 22-player squads, opening-squad freeze, fixture assignment, ordinary competition/round creation through to weekly-selection readiness | PASS |
| C | `opening-squads-frozen.dump` / Round 1 checkpoint lineage | Opening Round/Round 1 setup and selections, `early-1` and `main` lockout activation, calculation/review/finalisation/publication | PASS (one disposable-checkpoint AFL-round-id setup correction; not an application defect) |
| D | `first-half-complete-20260907T171603Z.dump` | First-half migration to current schema; 9-to-20-round continuation; Round 10 full weekly lifecycle; post-Round-10 Scorer surface exposing the mid-season draft entry point; a deliberately misconfigured `main`/`selective` trigger correctly rejected by preflight and corrected via **Remove this trigger** | PASS |

Full narrative and citations: issue #224 comments, 2026-09-20 and
2026-09-21.

**Scope note:** Stages A-D cover preseason through the Round 10 -> mid-
season-draft handoff. They do not re-run Rounds 11-20, Finals or SuperScore
on current code; those remain **replay-proven at their own historical
baseline** (second-half close and `5561a63`, the Finals/SuperScore closeout
merge) rather than current-code-regression-proven. No evidence found during
this review suggests a regression in that range -- the commits since
`5561a63` are scoped to preseason/mid-season-draft UX (#225-#232) -- but
this document does not claim a regression pass that was not run.

## Mid-season draft: two distinct kinds of evidence

The mid-season draft domain has been proven twice, in different ways, and
both matter for different reasons:

1. **The original 2026 historical replay** proved the domain rules and
   audit behaviour end-to-end, but did so through the CLI: the operator
   ran `scripts/replay_2026_midseason_draft.py`-style commands and
   routinely translated UUIDs to team/player names by hand
   (`2026-second-half-replay/ux-findings.md`).
2. **A current-code browser acceptance pass** (issue #224, 2026-09-20
   comment), run against a disposable `bbbffl-midseason-draft` environment
   restored from the preserved pre-midseason checkpoint and independently
   migrated from `0027_midseason_draft` to current schema, subsequently
   proved the *complete* browser workflow: discoverable setup from normal
   Admin/Scorer surfaces, trigger-round configuration, human-readable
   ordinary-competition selection, ladder preview/reverse-order
   confirmation/freeze, Coach and Scorer delisting (submit/withdraw),
   correct full-squad display, delistings lock and vacancy-based table
   generation, Coach-only draft-board access with read-only off-turn
   behaviour and private shortlists, Scorer/Admin proxy selection, browser
   polling for turn changes, and human-readable trade entry/approval/
   rejection -- with no UUID/CLI/database knowledge required.

Per issue #224's own comment, the former #181 v0.1 blocker "should now be
treated as replay/staging-proven on current code, subject to the normal
final CI/release checks." This document adopts that conclusion. Richer
trade negotiation, timers, auto-pick/proxy automation and broader polish
remain v0.2 candidates.

## Domain readiness matrix

### Season setup, activation and completion

| Capability | Status | Evidence |
|---|---|---|
| Season identity, coaches and season entries (Season Centre) | Replay-proven (all three phases) + automated-test-proven | `docs/season-centre.md`; all three evidence directories |
| Fresh-season player pool population and rules/ordinary-competition/round creation | **Outstanding / blocks v0.1** | Season Centre creates the season, coaches and entries, but the season/replay databases behind every 2026 replay phase were populated by `scripts/bootstrap_round1_2026.py`/`bootstrap_2026_first_half.py` and `scripts/replay_2026_draft.py` -- the only callers of `app.player_pool.PlayerPoolRepository.refresh_player`, and (per this review's route inventory) the only code that creates a fresh ordinary rules version/competition/rounds outside tests. Those scripts explicitly refuse to run under `BBBFFL_ENVIRONMENT=production`. A brand-new 2027 production database therefore has no way to populate its player pool or create its ordinary competition/rounds through any production-safe surface, browser or CLI. Found by Codex review on this PR (P1); confirmed against the current route/script inventory. Not previously named as a v0.1 item in issue #224's own candidate list -- the 2026 replay never needed this path because every replay phase started from a script-seeded or restored database, never a fresh production bootstrap. |
| Explicit `setup -> active` operational gate | **Outstanding / blocks v0.1** | The 2026 replay season stayed in `setup` through the entire season and was only transitioned via the supported `SeasonRepository.transition_lifecycle` CLI/domain call at closeout (`2026-finals-replay/workflow-findings.md` finding 10). This review's current route inventory (`app/routes/`) found no browser action that performs this transition. Explicitly named as a "likely v0.1" item in issue #224. |
| Season completion (`active -> completed`) and Premiership/Wooden Spoon award creation | Domain/test-proven and replay-proven in a non-production replay environment; **no viable production entry point today** | `app.season_completion.complete_season` is fully domain- and test-proven and was exercised successfully in the replay (`2026-finals-replay/provenance-manifest.md`), but the only wired entry point, `scripts/season_completion_2026.py`, explicitly refuses to run at all while `BBBFFL_ENVIRONMENT=production` (its own production guard). This review's route inventory found no browser route calling `complete_season`/`preview_complete_season` either. A live 2027 production deployment therefore currently has **no way to complete a season** through any surface. Not previously named as a v0.1 item in issue #224's own candidate list; recorded here as a finding from this documentation review's inspection of the current route/script inventory, not from replay evidence. |
| Archival verification (`scripts/season_archival_checkpoint_2026.py`) | Replay-proven as a non-production operator/CLI recovery procedure; **same production guard applies** | Archival verification and a post-completion write-fence smoke test both passed in the replay environment (`2026-finals-replay/workflow-findings.md` findings 14-15), but this script's `verify` subcommand -- read-only, never mutating -- also refuses to run while `BBBFFL_ENVIRONMENT=production`, confirmed in this review's inspection of its `main()`. A production deployment cannot currently run even the read-only archival check. This compounds the season-completion gap above rather than mitigating it, and should be resolved together with it. |

### Preseason draft

| Capability | Status | Evidence |
|---|---|---|
| Draft order, snake picks, pick ownership, opening-squad freeze, preseason trades | Replay-proven (first-half) + current-code regression-proven (Stages A-B) | `2026-first-half-replay/`; Stage A/B above |
| Coach-facing browser draft access | Current-code regression-proven | Issue #229/PR #230; Stage A above |
| Scorer/Admin proxy picks aligned with mid-season proxy workflow | Current-code regression-proven | Issue #231/#232; Stage A above |
| Private shortlists | Current-code regression-proven | Stage A above |

### Ordinary weekly operation

| Capability | Status | Evidence |
|---|---|---|
| Round mapping, staged selective/main lockout, submission, calculation, review, publication, ladder accumulation | Replay-proven (Rounds 1-9 and 11-20) + current-code regression-proven (Round 1 and Round 10, Stages C-D) | All three evidence directories; Stages C-D above |
| Audited locked-lineup correction and missed-submission adjudication | Replay-proven | `2026-first-half-replay/workflow-findings.md` |
| Carry-forward (deliberately conservative; never merges rejected draft content) | Replay-proven | `2026-first-half-replay/workflow-findings.md`, Round 9 finding |
| Bye-player selection correctness (hard-block submission, editable pre-lockout) | Replay-proven | Issue #185/#186; `2026-second-half-replay/ux-findings.md` |
| Final-round dashboard/attention-queue presentation | Deferred / v0.2 | `2026-first-half-replay/ux-findings.md`, "Final rounds should not appear as blockers" |
| Preflight default-value feedback, trigger-key convention | Deferred / v0.2 | `2026-first-half-replay/ux-findings.md` |
| Australian date presentation alongside UTC | Deferred / v0.2 | `2026-first-half-replay/ux-findings.md`; `2026-second-half-replay/ux-findings.md` |

### Lockouts

| Capability | Status | Evidence |
|---|---|---|
| Selective/main staged lockout, vacancy-locking, trigger materialisation on read/submit | Replay-proven + current-code regression-proven (Stage C) | `2026-first-half-replay/workflow-findings.md`; Stage C above |
| Fail-closed rejection of a misconfigured trigger, with a browser correction path | Current-code regression-proven | Stage D above (`Exactly one main/remaining lockout trigger must be configured`, **Remove this trigger**) |
| Bye-player invalid-selection vs lock-state separation | Replay-proven | Issue #185/#186 |

### Coach authentication, ownership and privacy

| Capability | Status | Evidence |
|---|---|---|
| Login/session/CSRF mechanism, own-team-only writes | Automated-test-proven, and replay-proven for the sessions actually exercised | `docs/coach-authentication.md`; two authenticated Coach accounts retained through the final two Finals/SuperScore replay rounds, with verified Finals eligibility and cross-Coach private-lineup isolation (`2026-finals-replay/workflow-findings.md` finding 12) |
| Initial coach credential provisioning (onboarding every coach before their first login) | **Outstanding / blocks v0.1** | The only wired operation is `POST /api/admin/coach-credential` (`app/routes/admin.py`), a JSON API gated by `X-Admin-Token` with no browser form calling it; `docs/coach-authentication.md` itself directs the operator to "a short script/`curl` invocation" against it. This is routine onboarding for every coach at the start of a season, not an exceptional recovery task, so it fails the normal-user criterion. Found by Codex review on this PR (P1); confirmed against the current route inventory. Not previously named as a v0.1 item in issue #224's own candidate list. |
| Coach self-service mid-season delisting | Current-code regression-proven | Issue #226, `app/routes/coach_delisting.py`; part of the mid-season draft acceptance pass above |
| Routine full-season individual Coach self-service for every weekly lineup | Automated-test-proven; **not the primary path replay-exercised** | The second-half/finals replay used delegated (Scorer/Admin proxy) entry for most rounds as "a useful proxy for future coach operation" (`2026-second-half-replay/workflow-findings.md`), reserving direct Coach sessions mainly for Finals. This is a reasonable substitute given delegated entry uses the identical validated pathway, but it means a non-technical Coach's own weekly session has had less direct replay exercise than the Scorer's. |

### Delegated Scorer operation

| Capability | Status | Evidence |
|---|---|---|
| Proxy lineup entry, correction, adjudication for exceptional cases | Replay-proven extensively, all phases | All three evidence directories |
| Scorer Operations Dashboard, next-safe-action, attention queue | Replay-proven + current-code regression-proven | PR #159; Stage D above |
| Scorer dashboard card layout / workflow-guidance polish | Deferred / v0.2 | `2026-first-half-replay/ux-findings.md`; `2026-finals-replay/ux-findings.md` finding 13 |

### Ladder and results

| Capability | Status | Evidence |
|---|---|---|
| Points/percentage/PF ordering, exact-equality escalation | Replay-proven | Both `round-results.md` records; ladder order verified round-by-round |
| Public ladder future-round subtitle correctness | Replay-proven | Issue #180 |
| PPG display without becoming a tiebreak criterion | Deferred / v0.2 (guard against regression) | `2026-first-half-replay/ux-findings.md` |

### Mid-season draft

See the dedicated section above. Summary: **current-code regression-proven
(browser)**; no longer a v0.1 blocker per issue #224's own conclusion,
subject to normal CI/release checks. Richer trade negotiation, timers and
auto-pick automation are v0.2.

### Finals

| Capability | Status | Evidence |
|---|---|---|
| Bracket generation, seeding (from the mathematical ladder, or the 2026-only historical snapshot), progression, publication | Replay-proven at the Finals/SuperScore phase's own baseline (`5561a63`) | `2026-finals-replay/` |
| Finals preflight discoverability and the paired Finals+SuperScore weekly open action | Replay-proven | Issue #211/#221, exercised as part of the same replay |
| Coach weekly-selection chronology across ordinary/Finals/SuperScore | Deferred / v0.2 | `2026-finals-replay/ux-findings.md` |
| Not re-run in the current-code regression | Staging/rehearsal-needed if a future change touches this area | See "Scope note" above |

### SuperScore

| Capability | Status | Evidence |
|---|---|---|
| SS1-SS4 lifecycle (stream/round/mapping, staged with Finals, review, publication) | Replay-proven at the Finals/SuperScore phase's own baseline | `2026-finals-replay/`; issue #194 |
| Completed-season write fence on SuperScore review rulings | Replay-proven | `2026-finals-replay/workflow-findings.md` finding 3 (Codex P1 on PR #207), finding 15 (real post-closeout smoke test) |
| Same-week Finals/SuperScore copy-to-draft convenience | Deferred / v0.2 | `2026-finals-replay/ux-findings.md` |
| Carry-forward vs private-draft presentation clarity | Deferred / v0.2 | `2026-finals-replay/ux-findings.md` |

### Completed-season write fence

| Capability | Status | Evidence |
|---|---|---|
| `guard_writable` fencing on ordinary/finals/SuperScore result-changing writes after completion | Replay-proven (real post-closeout smoke test) + automated-test-proven | `2026-finals-replay/workflow-findings.md` finding 15; `tests/test_superscore_round.py`, `tests/test_superscore_review_cli.py` |

### Public views

| Capability | Status | Evidence |
|---|---|---|
| Public Round Centre, ladder, published results (no login required) | Replay-proven | All three evidence directories |
| Public browsing of previous/upcoming rounds beyond the current one | Deferred / v0.2 | `2026-first-half-replay/ux-findings.md`, "low-priority roadmap item" |

### Backup/restore/archive

| Capability | Status | Evidence |
|---|---|---|
| Paired database-archive + checkpoint backup/restore mechanism itself | Replay-proven repeatedly (every phase boundary), as an operator/CLI procedure | All three provenance manifests |
| Production deployment backup/restore runbook rehearsal (real hosting environment, not a replay checkpoint) | **Staging/rehearsal-needed** | Named explicitly as a "likely v0.1" item in issue #224; the replay validated the archive/restore mechanism repeatedly but not under a real production deployment's operational conditions |

### Production/operations readiness

| Capability | Status | Evidence |
|---|---|---|
| CI quality gates (tests, lint/format, incremental type-check, migration integrity on SQLite and PostgreSQL, dependency audit, container build) | Automated-test-proven | `docs/ci-quality-gates.md` |
| Non-technical Scorer staging/beta rehearsal | **Staging/rehearsal-needed** | Recommended explicitly in `2026-finals-replay/ux-findings.md`, "Pre-2027 rehearsal"; not yet performed |
| Production backup/restore runbook rehearsal | **Staging/rehearsal-needed** | See "Backup/restore/archive" above |
| `Legacy Grand Final admin` link still reachable from ordinary Scorer Round Centre | **Outstanding / blocks v0.1 tidy-up** | Issue #233 (see below) |
| Post-trigger-round mid-season handoff prominence on the Scorer dashboard | **Outstanding / blocks v0.1 tidy-up** | Issue #233 (see below) |

## Remaining items before v0.1.0

Using the "normal user, no routine CLI/UUID knowledge" criterion strictly,
the following are outstanding. None of them invalidates the replay or
current-code regression evidence above; the underlying domain workflows
they touch have already passed.

1. **Fresh-season player pool population and rules/ordinary-competition/
   round creation.** Every 2026 replay phase started from a script-seeded
   or restored database; a genuinely new 2027 production database has no
   production-safe way (browser or CLI) to populate its player pool or
   create its ordinary competition/rounds, since the only code that does
   so (`scripts/bootstrap_round1_2026.py`, `bootstrap_2026_first_half.py`,
   `replay_2026_draft.py`) refuses to run under
   `BBBFFL_ENVIRONMENT=production`. Found by Codex review on this PR (P1);
   confirmed against the current route/script inventory. Arguably the most
   fundamental of the gaps recorded here: without it, a 2027 season cannot
   be started at all through a supported production path.
2. **Initial coach credential provisioning.** `POST /api/admin/coach-
   credential` is a JSON API with no browser form; onboarding every coach
   before their first login currently requires a script/`curl` invocation.
   Found by Codex review on this PR (P1); confirmed against the current
   route inventory.
3. **Explicit `setup -> active` season-activation gate** (browser action).
   Issue #224 candidate; no implementation issue filed as of this
   document.
4. **Season completion/archival production path.** `complete_season`/
   `preview_complete_season` are fully domain-proven, but their only wired
   entry point (`scripts/season_completion_2026.py`) refuses to run under
   `BBBFFL_ENVIRONMENT=production`, and the read-only archival verifier
   (`scripts/season_archival_checkpoint_2026.py verify`) does too. No
   browser route calls either domain function. A production 2027
   deployment currently has no way to complete a season or verify its
   archival checkpoint at all, through any surface. This is a stronger
   finding than a CLI/UUID inconvenience -- it is a missing capability in
   production. Identified during this documentation review's inspection of
   the current route/script inventory, not previously tracked by an issue
   as of this document.
5. **Issue #233 -- two Scorer UX cleanups found during the Round 10 ->
   mid-season regression:**
   - remove the obsolete `Legacy Grand Final admin` link from the ordinary
     Scorer Round Centre;
   - give the post-trigger-round mid-season handoff cue proper prominence
     in the Scorer's Next-safe-action/attention-queue area, instead of
     leaving a future ordinary-round preflight as the most visible action.

   **This documentation PR does not implement #233.** The underlying
   Round 10 -> mid-season-draft workflow itself already passed regression
   (Stage D); #233 is release tidy-up, not a re-open of that finding.
6. **Non-technical Scorer staging/beta rehearsal**, and **a production
   backup/restore runbook rehearsal against a real deployment** (as
   distinct from the replay's repeatedly-proven checkpoint mechanism).

Items 1-2 were surfaced by Codex's review of this PR, not by the 2026
replay itself -- the replay never needed a fresh-production-bootstrap or
first-time-credential path, since every phase started from a script-seeded
or restored database with credentials already in place. They sharpen this
document's v0.1 picture considerably (a brand-new production season
currently cannot be started or staffed through any supported surface) but
do not contradict any replay PASS verdict recorded elsewhere in this
document or in `docs/evidence/2026-full-season-replay-summary.md`: no
replay claim concerned fresh-production bootstrap in the first place.

Everything else catalogued above as "Deferred / v0.2 candidate" is
convenience/polish under the stated criterion and does not block
`v0.1.0`.

## What this document does not claim

- It does not claim Finals or SuperScore have been re-verified on the
  exact current commit; see the Stage A-D scope note.
- It does not claim the season-activation and season-completion gaps
  above were found by the 2026 replay; they were identified by this
  review's own inspection of the current route inventory, and are labelled
  accordingly rather than attributed to replay evidence that does not
  exist.
- It does not claim #233 is fixed. It is recorded as outstanding tidy-up.
- It does not claim a production staging rehearsal has occurred.
- It does not treat any v0.2/deferred item as blocking `v0.1.0`.
