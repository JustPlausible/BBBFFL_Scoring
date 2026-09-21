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
| A | The preserved pre-pick-1 preseason restore point, migrated from `0022_acting_context` | Coach-facing pre-season draft access (#229/#230), Scorer/Admin proxy picks (#231), private shortlists, immediate unavailability, fail-closed turn ownership | PASS |
| B | The preserved picks-201-220 preseason restore point | Picks 201-220 (mixed Coach/proxy), shortlist updates, draft navigation, finalisation, ten 22-player squads, opening-squad freeze, fixture assignment, ordinary competition/round creation through to weekly-selection readiness | PASS |
| C | The preserved opening-squads-frozen / Round 1 checkpoint lineage | Opening Round/Round 1 setup and selections, `early-1` and `main` lockout activation, calculation/review/finalisation/publication | PASS (one disposable-checkpoint AFL-round-id setup correction; not an application defect) |
| D | The preserved first-half-complete recovery point | First-half migration to current schema; 9-to-20-round continuation; Round 10 full weekly lifecycle; post-Round-10 Scorer surface exposing the mid-season draft entry point; a deliberately misconfigured `main`/`selective` trigger correctly rejected by preflight and corrected via **Remove this trigger** | PASS |

Per this repository's evidence policy (`docs/evidence/2026-first-half-replay/README.md`'s "Evidence policy"), backup/checkpoint filenames are never committed; only their identity/boundary description is recorded here, matching how the phase provenance manifests describe the same boundaries.

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
| Season Centre browser routes exist (`POST /seasons`, `POST /coaches`, `POST /{season_id}/entries`) and are readiness-proven for viewing/administering an already-created season | Automated-test-proven; **creation itself is not replay-proven** | `docs/season-centre.md`. The 2026 replay's season/coach/entry records were created by the CLI bootstrap scripts (`scripts/bootstrap_round1_2026.py` et al.), not by exercising these Season Centre creation routes through a browser; every phase after the first started from a restored database where these records already existed. Found by Codex review on this PR (P2); an earlier draft of this document conflated the persisted data's existence with the browser creation workflow having been exercised. |
| Fresh-season player pool population and rules/ordinary-competition/round creation | **Outstanding / blocks v0.1**, plus a **separate confirmed safety gap** | Season Centre creates the season, coaches and entries, but the season/replay databases behind every 2026 replay phase were populated by `scripts/bootstrap_round1_2026.py`, `scripts/bootstrap_2026_first_half.py` and `scripts/replay_2026_draft.py` -- the only callers of `app.player_pool.PlayerPoolRepository.refresh_player`, and (per this review's route inventory) the only code that creates a fresh ordinary rules version/competition/rounds outside tests. `bootstrap_round1_2026.py` and `replay_2026_draft.py` both explicitly refuse to run under `BBBFFL_ENVIRONMENT=production`. **`scripts/bootstrap_2026_first_half.py` does not** -- confirmed by inspecting its `main()` (no `BBBFFL_ENVIRONMENT` check anywhere in the file) after Codex review correctly flagged this document's earlier claim that all three scripts were production-guarded as wrong. This is a real, separate finding beyond a documentation gap: an operator could run this specific mutating script against a live production database and it would not refuse. This PR does not add the guard, since it is a documentation PR and the fix is an application code change (see the PR discussion for the recommendation to file a follow-up issue). A brand-new 2027 production database has no *safe* way to populate its player pool or create its ordinary competition/rounds through any production-safe surface, browser or CLI. Found by Codex review on this PR (P1, then P2 for the guard-claim correction); confirmed against the current route/script inventory. Not previously named as a v0.1 item in issue #224's own candidate list -- the 2026 replay never needed this path because every replay phase started from a script-seeded or restored database, never a fresh production bootstrap. |
| Provisional (not-yet-`afl-api`) player creation and later canonical reconciliation | **Outstanding / blocks v0.1 if it occurs** | Fixing the player-pool population workflow above still would not let a live 2027 season include a legitimate rookie or mid-season recruit `afl-api` does not yet represent: migration `0006_player_pool_ownership` requires a positive, non-null `canonical_player_id`, and `app.player_pool.PlayerPoolRepository` exposes only canonical `refresh_player` ingestion. `docs/plans/2027-season-model.md` requires provisional creation followed by audited canonical reconciliation, but no domain function, CLI or browser route implements it. Conditional in the same sense as the Opening Round item below: it only blocks a season that actually needs to add such a player, but that season would currently have no supported way to do so. Found by Codex review on this PR (P1); confirmed against migration `0006` and `app.player_pool`. |
| Explicit `setup -> active` operational gate | **Outstanding / blocks v0.1** | The 2026 replay season stayed in `setup` through the entire season and was only transitioned via the supported `SeasonRepository.transition_lifecycle` CLI/domain call at closeout (`2026-finals-replay/workflow-findings.md` finding 10). This review's current route inventory (`app/routes/`) found no browser action that performs this transition. Explicitly named as a "likely v0.1" item in issue #224. |
| Season completion (`active -> completed`) and Premiership/Wooden Spoon award creation | Domain/test-proven and replay-proven in a non-production replay environment; **no viable production entry point today** | `app.season_completion.complete_season` is fully domain- and test-proven and was exercised successfully in the replay (`2026-finals-replay/provenance-manifest.md`), but the only wired entry point, `scripts/season_completion_2026.py`, explicitly refuses to run at all while `BBBFFL_ENVIRONMENT=production` (its own production guard). This review's route inventory found no browser route calling `complete_season`/`preview_complete_season` either. A live 2027 production deployment therefore currently has **no way to complete a season** through any surface. Not previously named as a v0.1 item in issue #224's own candidate list; recorded here as a finding from this documentation review's inspection of the current route/script inventory, not from replay evidence. |
| Archival verification (`scripts/season_archival_checkpoint_2026.py`) | Replay-proven as a non-production operator/CLI recovery procedure; **same production guard applies** | Archival verification and a post-completion write-fence smoke test both passed in the replay environment (`2026-finals-replay/workflow-findings.md` findings 14-15), but this script's `verify` subcommand -- read-only, never mutating -- also refuses to run while `BBBFFL_ENVIRONMENT=production`, confirmed in this review's inspection of its `main()`. A production deployment cannot currently run even the read-only archival check. This compounds the season-completion gap above rather than mitigating it, and should be resolved together with it. |

### Preseason draft

| Capability | Status | Evidence |
|---|---|---|
| Draft order, snake picks, pick ownership, opening-squad freeze, preseason trades, once a draft already exists | Replay-proven (first-half) + current-code regression-proven (Stages A-B) | `2026-first-half-replay/`; Stage A/B above |
| Coach-facing browser draft access | Current-code regression-proven | Issue #229/PR #230; Stage A above |
| Scorer/Admin proxy picks aligned with mid-season proxy workflow | Current-code regression-proven | Issue #231/#232; Stage A above |
| Private shortlists | Current-code regression-proven | Stage A above |
| Draft *initialization* for a fresh season (squad-limit configuration, initial draft-order acceptance) | **Outstanding / blocks v0.1** | `app.player_pool.OwnershipRepository.configure_squad_limit` and `app.draft.DraftRepository.accept_order` have no browser route; their only non-test callers are `scripts/bootstrap_round1_2026.py` and `scripts/replay_2026_draft.py`, both production-guarded. Stages A-B above regression-tested *continuing* an already-bootstrapped draft, not starting one. Same root cause as the fresh-season bootstrap gap above; found by Codex review on this PR (P1). |

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
| Points/percentage/PF ordering (no exact equality encountered) | Replay-proven | Both `round-results.md` records; ladder order verified round-by-round, every 2026 replay ladder resolved without an exact tie at any boundary that mattered |
| Any exact ladder equality, anywhere on the ladder, once Finals seeding must fall back to the mathematical ladder (no 2026-style snapshot) | **Outstanding / blocks v0.1 if it occurs** | Not replay-proven, and not resolvable through any current surface. `app.finals.FinalsBracketRepository._resolve_seed` collects every tied `LadderRow` across all ten teams and raises `UnresolvedLadderTieError` if that list is non-empty at all -- an exact tie anywhere on the ladder blocks bracket creation via the ladder-seed path, not only a tie at the finals-qualification cutoff. `app.season_awards`'s wooden-spoon derivation separately refuses specifically on a last-place tie, blocking season completion. Both explicitly state resolution "requires an explicit, audited Scorer/competition-governance decision", but no such recording mechanism exists anywhere in the codebase (browser, CLI, or bare domain function). Found by Codex review on this PR (P2, then a second P2 round correcting the scope from "cutoff/last-place only" to "anywhere on the ladder"); confirmed by inspecting `_resolve_seed`'s full tied-row collection. |
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
| Bracket progression and publication, once a bracket already exists | Replay-proven at the Finals/SuperScore phase's own baseline (`5561a63`) | `2026-finals-replay/` |
| Bracket seeding from the 2026-only historical snapshot | Replay-proven, but **not the path any future season uses** | `2026-finals-replay/provenance-manifest.md` records `seed_source: snapshot`; `scripts/finals_bracket_2026.py` explicitly refuses to create a bracket unless that snapshot already exists |
| Bracket seeding from the live mathematical ladder (the path every 2027 season without a 2026-style historical snapshot will actually use) | **Automated-test-proven only; not replay-proven** | `app.finals.FinalsBracketRepository.create_bracket`'s ladder fallback (`_resolve_seed`, `seed_source == "ladder"`) was never exercised by the 2026 replay -- `scripts/finals_bracket_2026.py` deliberately refuses to take that branch. Only `tests/test_finals*.py` cover it. Found by Codex review on this PR (P2); an earlier draft of this document conflated the two seed sources under one "replay-proven" row. |
| Finals preflight discoverability and the paired Finals+SuperScore weekly open action | Replay-proven | Issue #211/#221, exercised as part of the same replay |
| **Finals competition-stream and bracket creation** (the first entry into Finals from a live, completed ladder) | **Outstanding / blocks v0.1** | `app.finals.FinalsBracketRepository.create_bracket` has no browser route; every `app/routes/finals_preflight.py` route takes an already-existing `bracket_id`. Its only non-test caller is `scripts/finals_bracket_2026.py`, which is production-guarded, depends on the 2026-only historical seeding snapshot for its non-ladder path, and itself requires an already-existing `finals`-typed `competition_stream` id as an argument. That stream is one level further back: `SeasonRepository.create_competition(..., "finals")` has no non-test caller outside `scripts/bootstrap_round1_2026.py` (which creates only an `"ordinary"` stream, not a `"finals"` one) -- nothing in this repository currently creates a finals competition stream for a fresh season, production-safe or otherwise. A live 2027 season reaching the end of Round 20 currently has no production-safe way to start Finals at either level. Found by Codex review on this PR (P1, then P2 for the deeper stream-creation layer). |
| Coach weekly-selection chronology across ordinary/Finals/SuperScore | Deferred / v0.2 | `2026-finals-replay/ux-findings.md` |
| Not re-run in the current-code regression | Staging/rehearsal-needed if a future change touches this area | See "Scope note" above |

### SuperScore

| Capability | Status | Evidence |
|---|---|---|
| SS1-SS4 round lifecycle (mapping, staged open with Finals, review, publication), once the stream and rounds already exist | Replay-proven at the Finals/SuperScore phase's own baseline | `2026-finals-replay/`; issue #194 |
| **SuperScore stream and SS1-SS4 round creation** | **Outstanding / blocks v0.1** | `app.superscore_round.ensure_stream`/`ensure_round` have no browser route; the paired Finals+SuperScore browser action (`open_finals_and_superscore_week`, issue #211/#221) sets up and opens a round that must already exist, it does not create the stream or the logical SS-round. The only non-test callers of `ensure_stream`/`ensure_round` are in `scripts/superscore_round_2026.py`, production-guarded. `app.season_completion`'s own completion check refuses unless the stream and all four SS rounds exist and are final, so this also blocks season completion for a season that never had these created. Found by Codex review on this PR (P1). |
| Completed-season write fence on SuperScore review rulings | Replay-proven | `2026-finals-replay/workflow-findings.md` finding 3 (Codex P1 on PR #207), finding 15 (real post-closeout smoke test) |
| Cross-round SuperScore standings, prize-allocation configuration, and a separate SuperScore records/history context | **Deferred / v0.2, not implemented** | `app.season_award` accepts only `premiership`/`wooden_spoon` award kinds; `app.superscore_round`'s own module docstring states it "deliberately does **not** implement ... cumulative/aggregate standings across the four rounds". The retained roadmap's package 38 lists this as required domain scope, but each SuperScore round's individual result is proven published (replay-proven above) independently of this aggregate/prize/records layer. Not a v0.1 blocker under the stated criterion: a season can complete SS1-SS4 without it, and the gap is presentation/records depth, not an inability to run the round. Found by Codex review on this PR (P2); confirmed against `app.season_award` and `app.superscore_round`'s own docstring. |
| Same-week Finals/SuperScore copy-to-draft convenience | Deferred / v0.2 | `2026-finals-replay/ux-findings.md` |
| Carry-forward vs private-draft presentation clarity | Deferred / v0.2 | `2026-finals-replay/ux-findings.md` |

### Completed-season write fence

| Capability | Status | Evidence |
|---|---|---|
| `guard_writable` fencing on SuperScore review-ruling writes after completion | Replay-proven (real post-closeout smoke test: an attempted SS4 DNP mutation via `scripts.superscore_review_2026` was refused, verified unchanged on re-check) | `2026-finals-replay/workflow-findings.md` finding 15 |
| The same `guard_writable` fencing on ordinary-round and Finals result-changing writes after completion | Automated-test-proven only -- **not separately replay-exercised** | The post-closeout smoke test (finding 15) attempted only the one SuperScore mutation; no ordinary or Finals write was attempted post-completion during the replay. `tests/test_superscore_round.py`, `tests/test_superscore_review_cli.py`, and the equivalent ordinary/finals test modules cover the shared mechanism. Found by Codex review on this PR (P2); an earlier draft of this document generalised the one exercised case across all three streams. |

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
| Reproducible production deployment topology, TLS/reverse-proxy setup, dependency-readiness probe, structured alerting/logging, scheduled backups with an accepted RPO/RTO, and a rollback runbook | **Outstanding / blocks v0.1** | `GET /health` (`app/routes/health.py`) returns only `{"status": "ok"}` -- a process-liveness check with no database/dependency probe. The repository has a generic `Dockerfile` and the Compose-first local/rehearsal workflow (`docs/round1-rehearsal.md`), but no reproducible production deployment topology, TLS/reverse-proxy configuration, structured alerting, or rollback runbook. **Backups today are manual-only**: every backup taken during the 2026 replay was an ad hoc `pg_dump` run by the operator immediately before a specific boundary, never a scheduled job -- a repo-wide search found no cron/systemd-timer/scheduler configuration anywhere. The "Backup/restore/archive" rehearsal item above proves a manually created archive can be restored; it does not establish that a live production season is protected between backups, since nothing currently takes one on a schedule. The original roadmap's package 39 already marked all of this P0/required for 2027 (roadmap section 2, "Health/observability: ... Missing operationally"); it was never closed and is carried forward here as current-state fact. Found by Codex review on this PR (P1, across two rounds: deployment/observability controls, then specifically scheduled backups); confirmed by inspecting `app/routes/health.py` and searching the repository for the described controls (none exist). |
| Notification delivery (lockout/missing-team alerts) and a live-season incident/manual-fallback operations runbook | **Deferred / v0.2** | No notification adapter or delivery configuration exists in `app/` (roadmap package 40's own scope); no incident/fallback runbook exists in the repository either. Unlike the deployment-controls row above, this does not block running an individual round end-to-end -- a season can operate without automated reminders, with the Scorer relying on the existing dashboards/attention queue instead -- so it is classified as deferred rather than outstanding, consistent with the original roadmap treating packages 39 (P0, blocking) and 40 (P1, "final launch gate" but reminder-level) differently. Found by Codex review on this PR (P2); confirmed by inspecting `app/` for any notification adapter (none exists). |
| `Legacy Grand Final admin` link still reachable from ordinary Scorer Round Centre | **Outstanding / blocks v0.1 tidy-up** | Issue #233 (see below) |
| Post-trigger-round mid-season handoff prominence on the Scorer dashboard | **Outstanding / blocks v0.1 tidy-up** | Issue #233 (see below) |

## Remaining items before v0.1.0

Using the "normal user, no routine CLI/UUID knowledge" criterion strictly,
the following are outstanding. None of them invalidates the replay or
current-code regression evidence above; the underlying domain workflows
they touch have already passed.

1. **Fresh-season / new-phase initialization has no production-safe path,
   at every phase boundary.** Every 2026 replay phase started from a
   script-seeded or restored database, so this class of gap was invisible
   to the replay itself. Confirmed by this review and by Codex review on
   this PR (P1, across several review rounds) against the current
   route/script inventory. The only non-test callers of each of the
   following are 2026-specific operator scripts, with no browser route as
   an alternative:
   - player pool population and ordinary rules/competition/round creation
     (`scripts/bootstrap_round1_2026.py`, `scripts/bootstrap_2026_first_half.py`);
   - preseason draft initialization -- squad-limit configuration and
     initial draft-order acceptance (`scripts/bootstrap_round1_2026.py`,
     `scripts/replay_2026_draft.py`);
   - if 2027 retains an AFL Opening Round: the club-to-compensating-bye
     rules deferred scoring depends on, accepted only by
     `app.replay_bootstrap`'s `accept_locked` path -- Season Centre's
     browser routes manage nominations against an existing rule but do not
     create one;
   - Finals competition-stream creation (`SeasonRepository.create_
     competition(..., "finals")` has no non-test caller at all) and, one
     level above it, bracket creation, the first entry into Finals from a
     completed ladder (`scripts/finals_bracket_2026.py`, which requires an
     already-existing finals stream id and also depends on the 2026-only
     seeding snapshot for its non-ladder path);
   - SuperScore stream and SS1-SS4 round creation
     (`scripts/superscore_round_2026.py`) -- this also blocks season
     completion for any season that never had these rounds created, since
     `app.season_completion` requires all four to exist and be final.

   Most of these scripts explicitly refuse to run under `BBBFFL_ENVIRONMENT
   =production` (confirmed individually). **`scripts/bootstrap_2026_first_
   half.py` is the one exception: it has no such guard at all**, confirmed
   by inspecting its `main()` after Codex review correctly caught an
   earlier draft of this document wrongly claiming otherwise. That is a
   real, separate safety gap in the script itself, not only a documentation
   accuracy issue, and this documentation-only PR does not add the missing
   guard -- see the PR discussion for the recommendation to file a
   follow-up issue for that specific fix.

   Taken together, a genuinely new 2027 production season cannot be
   started, drafted, taken through an Opening Round, or carried through to
   Finals/SuperScore without either running a script against a production
   database (unsafe for the one script above, and a route around the
   guard everywhere else) or adding browser/production-safe equivalents of
   each `ensure_*`/`create_*`/`configure_*`/`accept_order`/`accept_locked`
   call above. This is the single largest gap this document records, and
   the most consequential correction this PR's review produced.
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
7. **Any exact ladder equality, anywhere on the ladder, once Finals must
   seed from the mathematical ladder** (no 2026-style historical
   snapshot). `_resolve_seed` refuses on *any* tied row, not only a tie at
   the finals cutoff; a last-place tie separately blocks season completion
   through `app.season_awards`. No 2026 replay ladder happened to land on
   an exact tie, so this was never replay-exercised, and no domain
   function, CLI or browser route implements the "explicit, audited
   Scorer/competition-governance decision" both modules say resolution
   requires. Conditional in the same sense as the Opening Round item
   above: it only blocks a season that actually produces such a tie, but
   that season would currently have no way through Finals or completion
   at all. Found by Codex review on
   this PR (P2).
8. **Finals bracket seeding from the live mathematical ladder** -- the
   branch every 2027 season without a 2026-style historical snapshot will
   actually use -- is automated-test-proven only, not replay-proven; the
   2026 replay's own tooling deliberately always took the snapshot branch
   instead. This does not block v0.1 by itself (the domain logic is
   tested), but the readiness matrix no longer overstates it as
   replay-proven. Found by Codex review on this PR (P2).
9. **Provisional (not-yet-`afl-api`) player creation and canonical
   reconciliation.** A legitimate rookie or mid-season recruit not yet
   represented by `afl-api` cannot currently be added to a season's player
   pool at all -- migration `0006_player_pool_ownership` requires a
   positive, non-null `canonical_player_id`, and no domain function, CLI
   or browser route implements the provisional-creation-then-reconciliation
   path `docs/plans/2027-season-model.md` specifies. Conditional in the
   same sense as the Opening Round and ladder-equality items: it only
   blocks a season that actually needs to add such a player. Found by
   Codex review on this PR (P1).
10. **Reproducible production deployment topology, TLS/reverse-proxy
    setup, a dependency-readiness probe, structured alerting, scheduled
    backups with an accepted RPO/RTO, and a rollback runbook.**
    `GET /health` is process-liveness only; no deployment topology,
    TLS/reverse-proxy configuration, or rollback runbook exists in the
    repository beyond the generic `Dockerfile` and the local/rehearsal
    Compose workflow. Backups today are entirely manual `pg_dump` runs
    taken by an operator at a chosen boundary -- nothing schedules one, so
    a live production season is unprotected between manual runs. The
    original roadmap's package 39 already marked all of this P0/required
    and it was never closed -- carried forward here as current fact.
    Found by Codex review on this PR (P1, across two rounds). Notification
    delivery and an incident/fallback runbook (roadmap package 40) are a
    separate, related gap classified as deferred/v0.2 in the matrix above,
    since they do not block running an individual round.

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
- It does not claim `scripts/bootstrap_2026_first_half.py` is production-
  guarded. An earlier draft of this document said it was, alongside its
  sibling scripts; that was wrong, and is corrected here after Codex
  review caught it. This PR does not add the missing guard, since that is
  an application code change and this is a documentation PR.
- It does not treat any v0.2/deferred item as blocking `v0.1.0`.
