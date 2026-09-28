# 2027 live-season readiness

**Status:** current-state readiness assessment, 21 September 2026;
remaining item 1 (fresh-season/phase initialization) updated 25 September
2026 for issue #237; remaining item 3 (explicit `setup -> active`
season-activation gate) updated 27 September 2026 for issue #239; remaining
item 10 (production deployment/readiness/backup/rollback baseline) updated
27 September 2026 for issue #243; remaining item 11 (live `afl-api`
deployment validation) updated 27 September 2026 for issue #244; remaining
item 4 (season completion/archival production path) updated 28 September
2026 for issue #240; remaining item 7 (audited resolution for an exact
ladder tie) resolved 28 September 2026 by issue #241.
**Supersedes:** the current-state claims in
[`docs/roadmap/2027-season-roadmap.md`](roadmap/2027-season-roadmap.md),
which is now a historical planning baseline from 23 August 2026 -- see that
document's own status note. This document does not replace the roadmap's
sequencing/decision record, only its capability claims.
**Evidence base:** the completed 2026 full-season replay
([`docs/evidence/2026-full-season-replay-summary.md`](evidence/2026-full-season-replay-summary.md)
and the three phase evidence directories it synthesises), the targeted
current-code regression run under issue #224 (Stages A-D, below), the
repository's automated test suite, issue #243's disposable-environment
rehearsal
([`evidence/production-operations-rehearsal-2026-09-27.md`](evidence/production-operations-rehearsal-2026-09-27.md)),
and issue #244's live `afl-api` deployment validation
([`docs/afl-api-v1-contract.md`](afl-api-v1-contract.md#live-validation-status)).

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
| Fresh-season player pool population and rules/ordinary-competition/round creation | **Automated-test-proven + clean-database acceptance run (issue #237); staging/rehearsal-needed** | Issue #237 added the browser [Season setup](season-setup.md) page (`/admin/season-setup/{season_id}`, Scorer/Secretary/Admin): the player pool is populated/refreshed from the live afl-api season player list (`AflApiClient.get_season_players` -> `PlayerPoolRepository.refresh_season_pool`, one audited transaction, idempotent, year/AFL-season cross-checked, never from a stale cache), and the ordinary rules version, competition stream and Rounds 1-N are created in one audited transaction (`SeasonRepository.initialize_ordinary_competition`, idempotent, fail-closed on partial structure). Covered by `tests/test_season_setup*.py` and exercised against a clean disposable PostgreSQL database ([`evidence/season-setup-acceptance-2026-09-25.md`](evidence/season-setup-acceptance-2026-09-25.md)). The previously confirmed safety gap is closed: `scripts/bootstrap_2026_first_half.py` now refuses under `BBBFFL_ENVIRONMENT=production` before connecting to any database (`tests/test_bootstrap_2026_first_half_cli.py`). The live afl-api deployment itself is now confirmed reachable and contract-compatible, including the `get_season_players` endpoint specifically (issue #244, see the separate live afl-api validation row below) -- but not yet rehearsed end-to-end through this browser page against that deployment, and afl-api has not yet published a 2027 season for it to read. |
| Provisional (not-yet-`afl-api`) player creation and later canonical reconciliation | **Outstanding / blocks v0.1 if it occurs** | Fixing the player-pool population workflow above still would not let a live 2027 season include a legitimate rookie or mid-season recruit `afl-api` does not yet represent: migration `0006_player_pool_ownership` requires a positive, non-null `canonical_player_id`, and `app.player_pool.PlayerPoolRepository` exposes only canonical `refresh_player` ingestion. `docs/plans/2027-season-model.md` requires provisional creation followed by audited canonical reconciliation, but no domain function, CLI or browser route implements it. Conditional in the same sense as the Opening Round item below: it only blocks a season that actually needs to add such a player, but that season would currently have no supported way to do so. Found by Codex review on this PR (P1); confirmed against migration `0006` and `app.player_pool`. |
| Explicit `setup -> active` operational gate -- **resolved by issue #239** | Automated-test-proven; **staging/rehearsal-needed** | `GET`/`POST /api/scorer/season-activation/{season_id}` (`app/routes/season_activation.py`, page at `/scorer/season-activation/{season_id}`) is now the browser workflow: a read-only readiness preview (season entries, player pool/completed squads, ordinary competition structure, fixture-draw freeze, preseason draft finalisation and trade-window closure -- see [`season-activation.md`](season-activation.md)) followed by an explicit, reason-required Scorer/Administrator confirmation. Reuses the existing `season.lifecycle.changed` audit event and the completed-season write fence; the lower-level `SeasonRepository.transition_lifecycle` capability is unchanged. Available from the Season Centre, Season setup's nav bar and the Administrator Dashboard's additive "ready to activate" card. Covered by `tests/test_season_activation.py`/`tests/test_season_activation_api.py`. Not yet exercised against a real production deployment or a non-technical operator. |
| Season completion (`active -> completed`) and Premiership/Wooden Spoon award creation -- **resolved by issue #240** | Domain/test-proven and replay-proven in a non-production replay environment; **now automated-test-proven for the browser path; staging/rehearsal-needed** | `GET`/`POST /api/scorer/season-completion/{season_id}` (`app/routes/season_completion.py`, page at `/scorer/season-completion/{season_id}`) is now the browser workflow: a read-only readiness preview (every required finals week and SuperScore round's lifecycle state, naming any that are not yet `final`) followed by an explicit, reason-required Scorer/Administrator confirmation. Calls `app.season_completion.preview_complete_season`/`complete_season` unchanged -- the same six-step atomic transaction, Premiership/Wooden-Spoon recording and permanent completed-season write fence issue #195 already proved. A successful response shows the terminal lifecycle state, the completed-season version, the completion-event identifier and both awards (resolved to team names). See [`season-completion.md`](season-completion.md). Covered by `tests/test_season_completion.py`/`tests/test_season_completion_api.py`. Not yet exercised against a real production deployment or a non-technical operator. |
| Archival verification (`scripts/season_archival_checkpoint_2026.py`) -- **resolved by issue #240** | Replay-proven as a non-production operator/CLI recovery procedure; **now also automated-test-proven for a production-safe browser path; staging/rehearsal-needed** | `GET /api/scorer/season-completion/{season_id}/archival-verification` calls `app.season_archival.verify_season_completed_for_archival` unchanged -- read-only, never mutating, never migrating the schema -- and is production-safe by construction, independent of `scripts/season_archival_checkpoint_2026.py`, whose own `BBBFFL_ENVIRONMENT=production` refusal is untouched and remains in force for that 2026-replay-only script. See [`season-completion.md`](season-completion.md)'s "Archival verification" section. Covered by `tests/test_season_completion_api.py` (including proof that repeated calls change nothing). |

### Preseason draft

| Capability | Status | Evidence |
|---|---|---|
| Draft order, snake picks, pick ownership, opening-squad freeze, preseason trades, once a draft already exists | Replay-proven (first-half) + current-code regression-proven (Stages A-B) | `2026-first-half-replay/`; Stage A/B above |
| Coach-facing browser draft access | Current-code regression-proven | Issue #229/PR #230; Stage A above |
| Scorer/Admin proxy picks aligned with mid-season proxy workflow | Current-code regression-proven | Issue #231/#232; Stage A above |
| Private shortlists | Current-code regression-proven | Stage A above |
| Draft *initialization* for a fresh season (squad-limit configuration, initial draft-order acceptance) | **Automated-test-proven + clean-database acceptance run (issue #237); staging/rehearsal-needed** | The Season setup page's squad-limit and draft-order steps call `OwnershipRepository.configure_squad_limit` and `DraftRepository.accept_order` unchanged (team names on screen, no UUIDs), gated on ten entries, the ordinary competition, a squad limit and a sufficient pool; identical re-acceptance is a no-op, concurrent duplicates converge (PostgreSQL test). `tests/test_season_setup_api.py` drives a real Scorer session from a clean database to a Coach making Pick 1 on their own draft page. |

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
| Initial coach credential provisioning (onboarding every coach before their first login) | Resolved (issue #238) | `GET /admin/coach-credentials` (`app/routes/coach_credentials.py`) is now the normal browser workflow: an Administrator selects a coach by name/team (no `coach_id`, `curl`, script or database access) and sets or resets a password, getting a clear success/error result. The page shell itself is unauthenticated (so it stays reachable via the legacy `X-Admin-Token`, which a plain page load cannot attach as a header); its JS drives the CSRF-protected `GET`/`POST /api/admin/coach-credentials` JSON API, which calls the same `AuthenticationService.reset_password` the existing singular JSON API (`POST /api/admin/coach-credential`, kept for scripted/API use) already used. `tests/test_coach_credentials_api.py` covers authorization (Administrator-only, matching the JSON endpoint's existing authority -- the current capability model grants no Scorer credential-management capability), CSRF, provisioning, reset (including session revocation and an Administrator resetting their own credential), validation failures and audit attribution to the authenticated operator. |
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
| Any exact ladder equality, anywhere on the ladder, once Finals seeding must fall back to the mathematical ladder (no 2026-style snapshot), or at last place for the Wooden Spoon -- **resolved by issue #241** | Automated-test-proven; **staging/rehearsal-needed** | `app.ladder_tie_ruling` (migration `0037_ladder_tie_ruling`) is the audited manual-resolution path both `app.finals`/`app.finals_seeding` (via `resolve_full_ladder_order`) and `app.season_awards` (via `resolve_tie`) now consult before raising `UnresolvedLadderTieError`/`UnresolvedWoodenSpoonTieError` -- neither exception's raise condition changed (an exact tie with no fresh ruling still fails closed exactly as before), only the recovery path was added. An authorised Scorer/Administrator records the decided best-to-worst order for the exact tied group, with a mandatory reason, through the Scorer Operations "Ladder tie ruling" page (`/scorer/ladder-tie-ruling/{season_id}`, `app/routes/ladder_tie_ruling.py`) or its JSON API; the ruling is scoped to `(season_id, competition_id, through_round, tie_group)` and detects staleness by comparing its frozen `(matchup_id, official_version)` result-reference set against the ladder's current one, the same technique `app.finals`/`app.finals_seeding` already use for their own staleness checks. One recorded ruling is transparently reused by every consumer of that exact tie (Finals seeding and the Wooden Spoon alike) -- never a second mathematical tiebreaker, and never a fallback to team name, `season_entry_id`, or database order. Covered by `tests/test_ladder_tie_ruling.py`, `tests/test_ladder_tie_ruling_api.py`, and new cases in `tests/test_finals.py`/`tests/test_finals_seeding.py`/`tests/test_season_completion.py`/`tests/test_scorer_dashboard.py`. See [`ladder-progression.md`](ladder-progression.md#unresolved-exact-ties-audited-manual-ruling-issue-241). Not yet exercised against a real production deployment or a non-technical operator -- no 2026 replay ladder happened to reach this state, so this remains automated-test-proven only. |
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
| Ordinary (non-tied) bracket progression and publication, once a bracket already exists | Replay-proven at the Finals/SuperScore phase's own baseline (`5561a63`) | `2026-finals-replay/`; no tied Finals match occurred during the replay, per its round-results record |
| Tied-match progression (the higher frozen seed advances) | **Automated-test-proven only; not replay-proven** | No 2026 Finals week ended level, so this branch was never operationally exercised. Covered by `tests/test_finals.py` (`test_week2_pairing_derivation_for_every_week1_winner_combination`, `test_grand_final_pairing_and_tie_progression`). Found by Codex review on this PR (P2); an earlier draft of this document's broad "bracket progression" row implicitly overstated this conditional branch as replay-proven along with the exercised ordinary case. |
| Bracket seeding from the 2026-only historical snapshot | Replay-proven, but **not the path any future season uses** | `2026-finals-replay/provenance-manifest.md` records `seed_source: snapshot`; `scripts/finals_bracket_2026.py` explicitly refuses to create a bracket unless that snapshot already exists |
| Bracket seeding from the live mathematical ladder (the path every 2027 season without a 2026-style historical snapshot will actually use) | **Automated-test-proven only; not replay-proven** | `app.finals.FinalsBracketRepository.create_bracket`'s ladder fallback (`_resolve_seed`, `seed_source == "ladder"`) was never exercised by the 2026 replay -- `scripts/finals_bracket_2026.py` deliberately refuses to take that branch. Only `tests/test_finals*.py` cover it. Found by Codex review on this PR (P2); an earlier draft of this document conflated the two seed sources under one "replay-proven" row. |
| Finals preflight discoverability and the paired Finals+SuperScore weekly open action | Replay-proven | Issue #211/#221, exercised as part of the same replay |
| **Finals competition-stream and bracket creation** (the first entry into Finals from a live, completed ladder) | **Automated-test-proven + clean-database acceptance run (issue #237); staging/rehearsal-needed** | The Season setup page's Finals step (`FinalsBracketRepository.preview_ladder_seed` -> `ensure_finals_stream` -> `create_bracket`) is available only once every regular-season round is final and the live mathematical ladder is untied; it always seeds from the ladder and refuses a season carrying the 2026 historical snapshot. The existing fail-closed exact-tie behaviour is preserved, now with an audited recovery path (item 7, resolved by issue #241) rather than a permanent block. Not replay-proven: no live season has yet reached this boundary. |
| Coach weekly-selection chronology across ordinary/Finals/SuperScore | Deferred / v0.2 | `2026-finals-replay/ux-findings.md` |
| Not re-run in the current-code regression | Staging/rehearsal-needed if a future change touches this area | See "Scope note" above |

### SuperScore

| Capability | Status | Evidence |
|---|---|---|
| SS1-SS4 round lifecycle (mapping, staged open with Finals, review, publication), once the stream and rounds already exist | Replay-proven at the Finals/SuperScore phase's own baseline | `2026-finals-replay/`; issue #194 |
| **SuperScore stream and SS1-SS4 round creation** | **Automated-test-proven + clean-database acceptance run (issue #237); staging/rehearsal-needed** | The Season setup page's SuperScore step (`app.superscore_round.initialize_structure`: stream + SS1-SS4 in one audited transaction, idempotent) is available only once the Finals bracket exists; weekly mapping/setup/opening continues through the existing paired "Open week" action. |
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
| Scheduled (not merely manual) production PostgreSQL backups, with retention and failure visibility | **Automated-test-proven + disposable-environment rehearsal (issue #243); staging/rehearsal-needed on the real production host** | `compose.production.yaml`'s `backup` service (`deploy/production/scripts/backup_entrypoint.sh`/`backup_postgres.sh`) runs `pg_dump -Fc` on a cron schedule (default daily 02:15 UTC), prunes by configurable retention, names files unambiguously (`bbbffl-<db>-<UTC timestamp>.dump`), writes to a host bind mount outside every container's writable layer, and alerts (webhook + `CRITICAL` log) on failure. Rehearsed end-to-end in a disposable sandbox, including the failure path: [`evidence/production-operations-rehearsal-2026-09-27.md`](evidence/production-operations-rehearsal-2026-09-27.md) section D. Not yet run on a real production host against production-scale data. |
| Production restore procedure, verified beyond "exited zero" | **Automated-test-proven + disposable-environment rehearsal (issue #243); staging/rehearsal-needed on the real production host** | `deploy/production/scripts/restore_postgres.sh` restores into a named target database (refusing the live database name without an explicit override), rehearsed by backing up the production-topology rehearsal database and restoring it into a genuinely separate, disposable PostgreSQL container -- verified by matching `alembic_version` migration head, a representative application-written decision row, and its audit-trail entry, not merely the restore command's exit code. See [`production-operations.md#restore-procedure`](production-operations.md#restore-procedure) and the evidence document's section E. |

### Production/operations readiness

| Capability | Status | Evidence |
|---|---|---|
| CI quality gates (tests, lint/format, incremental type-check, migration integrity on SQLite and PostgreSQL, dependency audit, container build) | Automated-test-proven | `docs/ci-quality-gates.md` |
| Non-technical Scorer staging/beta rehearsal | **Staging/rehearsal-needed** | Recommended explicitly in `2026-finals-replay/ux-findings.md`, "Pre-2027 rehearsal"; not yet performed |
| Live `afl-api` deployment validation (the application's sole live data dependency for 2027) | **Substantially validated (issue #244, 2026-09-27); only 2027-season publication remains open** | `docs/afl-api-v1-contract.md`'s ["Live validation status"](afl-api-v1-contract.md#live-validation-status) section now records a positive live run: the configured deployment (`AFL_API_BASE_URL`/`AFL_API_KEY`, read only via `get_settings()`) is reachable and contract-compatible, every endpoint this document classifies "required now" was exercised against real 2026-season data (30 rounds, a full paginated player pool), and `/openapi.json` was compared with no incompatible difference. Match-lifecycle `status` and player-stat `lifecycle.finality` were confirmed live as genuinely independent signals (a `CONCLUDED` match temporarily reporting `finality="not_available"`, see below); the full four-state (`UPCOMING`/`LIVE`/`POSTGAME`/`CONCLUDED`) vocabulary itself, including `POSTGAME` specifically not collapsing into `CONCLUDED`, remains source/test-confirmed rather than live-observed, since every match in this run was already `CONCLUDED`. **Player-stats completeness across all 218 matches in season 85 is now confirmed** -- a first validation pass (~11:47 UTC) found three matches (the two Preliminary Finals and the Grand Final) with `finality="not_available"` and zero player rows, correctly recording season 85 as not fully populated and blocking packages 08/32; the upstream provider backfilled those three matches from the authoritative CFS source, and a second pass (~12:51 UTC), including a season-wide sweep of all 218 matches (not a single sample), confirmed every match now reports `finality="final"` with player rows present. This no longer blocks packages 08/32. **Credential validation is also now confirmed:** the validating session's own network path auto-authenticated every request regardless of the key sent, so its diagnostic run alone could not distinguish "the deployment accepts any key" from "a real key is enforced and already authenticated" -- but the operator independently ran the equivalent checks from the BBBFFL production Docker host (a network path that does not auto-authenticate) and confirmed `401` with no key, `401` with an invalid key, and `200` with the real configured key, exactly as the contract requires. **What remains open:** no 2027 season resource exists yet upstream at all, an expected off-season timing state the day after the 2026 Grand Final, not a BBBFFL gap. Season Setup (`app/season_setup.py`) does not depend on any season being flagged `is_current` -- it lists all seasons via `get_seasons()` and lets the operator select an explicit `afl_season_id` -- so this alone, not `is_current`, is what blocks it until afl-api publishes a 2027 season; an earlier draft of this row incorrectly named `AflApiClient.get_current_season()` (used only by the legacy Grand Final/SuperScore prototype's `app/service.py`, not by Season Setup) as the dependency. This remaining item is not a contract incompatibility. |
| Production backup/restore runbook rehearsal | **Automated-test-proven + disposable-environment rehearsal (issue #243); staging/rehearsal-needed on the real production host** | See "Backup/restore/archive" above |
| Reproducible production deployment topology, TLS/reverse-proxy setup, dependency-readiness probe, structured alerting/logging, scheduled backups with an accepted RPO/RTO, and a rollback runbook | **Implemented and disposable-environment-rehearsed (issue #243); staging/rehearsal-needed on the real production host** | `compose.production.yaml` defines a reproducible four-service topology (app, PostgreSQL, a Caddy TLS-terminating reverse proxy, and a scheduled-backup service), documented operator-by-operator in [`production-operations.md`](production-operations.md). `GET /health` (`app/routes/health.py`) remains a pure process-liveness check exactly as before; a new, separate `GET /health/ready` checks database connectivity and (only when `afl_mode == "live"`) afl-api connectivity, each bounded by a configurable timeout, never mutating state, never leaking a credential -- see `tests/test_health_api.py` and [`production-operations.md#readiness-vs-liveness`](production-operations.md#readiness-vs-liveness). Structured `CRITICAL`/`WARNING` logging now covers startup/config failure, database/migration failure, a dependency-readiness failure, an otherwise-unhandled application exception, and backup failure; a host-run `readiness_watch.sh` plus an optional alert webhook give a practical, reproducible alerting path (see [`production-operations.md#logging-and-alerting`](production-operations.md#logging-and-alerting)). Scheduled backups, the restore procedure, and both an application-only and a database-affecting rollback procedure are documented and were rehearsed end-to-end in a disposable sandbox environment, including the failure paths -- see [`evidence/production-operations-rehearsal-2026-09-27.md`](evidence/production-operations-rehearsal-2026-09-27.md) and [`production-operations.md#rollback-strategy`](production-operations.md#rollback-strategy) (which explains why a bare "`alembic downgrade` then restart the previous image" recipe is unsafe given this repository's forward-only migration refusal policy). RPO (24h) and RTO (4h) targets have been accepted by Steve as the production recovery targets for v0.1 (JustPlausible/BBBFFL_Scoring#249, 27 September 2026) -- see [`production-operations.md#rpo-and-rto`](production-operations.md#rpo-and-rto). None of the rest of this was rehearsed against the real production host, real DNS/TLS, or a non-technical operator -- see [`production-operations.md#what-remains-environment-specific`](production-operations.md#what-remains-environment-specific) for exactly what remains. This does not change the separately tracked live-`afl-api` validation row below, which is now substantially validated (issue #244) with only upstream 2027-season publication timing remaining. |
| Notification delivery (lockout/missing-team alerts) and a live-season incident/manual-fallback operations runbook | **Deferred / v0.2** | No notification adapter or delivery configuration exists in `app/` (roadmap package 40's own scope); no incident/fallback runbook exists in the repository either. Unlike the deployment-controls row above, this does not block running an individual round end-to-end -- a season can operate without automated reminders, with the Scorer relying on the existing dashboards/attention queue instead -- so it is classified as deferred rather than outstanding, consistent with the original roadmap treating packages 39 (P0, blocking) and 40 (P1, "final launch gate" but reminder-level) differently. Found by Codex review on this PR (P2); confirmed by inspecting `app/` for any notification adapter (none exists). |
| `Legacy Grand Final admin` link still reachable from ordinary Scorer Round Centre | Resolved | Issue #233: link removed from `/scorer/round-centre/{round_id}`; covered by `tests/test_round_review_api.py` |
| Post-trigger-round mid-season handoff prominence on the Scorer dashboard | Resolved | Issue #233: once the configured trigger round is final and no mid-season draft has started, the Scorer dashboard's Next safe action is **Open mid-season draft operations**; covered by `tests/test_scorer_dashboard.py` |

## Remaining items before v0.1.0

Using the "normal user, no routine CLI/UUID knowledge" criterion strictly,
the following are outstanding. None of them invalidates the replay or
current-code regression evidence above; the underlying domain workflows
they touch have already passed.

1. **Fresh-season / new-phase initialization -- addressed by issue #237.**
   The browser [Season setup](season-setup.md) page
   (`/admin/season-setup/{season_id}`, Scorer/Secretary/Administrator) is now
   the supported production path for every boundary this item listed, each
   reusing the existing domain function rather than the replay bootstrap:
   - player pool population/refresh from the live afl-api season player
     list, and ordinary rules version/competition/Rounds 1-N creation;
   - preseason draft initialization -- squad limit and initial draft-order
     acceptance through `DraftRepository.accept_order`;
   - Opening Round compensating-bye rules, derived from the live AFL fixture
     (round 0 participants and each club's first later bye), offered only
     when the fixture has an Opening Round and refused after Pick 1;
   - Finals stream and bracket creation from the live mathematical ladder,
     only after every regular-season round is final;
   - SuperScore stream and SS1-SS4 creation, only after the Finals bracket
     exists.

   Every step is idempotent or fails closed with a named conflict, is
   transactional and audited against the acting operator, and re-checks its
   prerequisites server-side. `scripts/bootstrap_2026_first_half.py` now has
   the production refusal its siblings already had.

   **Evidence level:** automated-test-proven (`tests/test_season_setup*.py`,
   including PostgreSQL concurrency) plus a clean disposable-database
   acceptance run ([`evidence/season-setup-acceptance-2026-09-25.md`](evidence/season-setup-acceptance-2026-09-25.md)). It is **not**
   replay-proven or staging-proven: it still needs the non-technical Scorer
   rehearsal (item 6) and depends on live afl-api validation (item 11) for
   the player-pool read. Conditional gaps adjacent to it remain separate:
   provisional players (item 9) and exact ladder ties (item 7).
2. **Initial coach credential provisioning -- resolved by issue #238.**
   `GET /admin/coach-credentials` (backed by the `GET`/`POST /api/admin/
   coach-credentials` JSON API) is now the browser workflow every
   Administrator onboarding a coach uses; see "Coach authentication,
   ownership and privacy" above.
3. **Explicit `setup -> active` season-activation gate -- resolved by issue
   #239.** `GET`/`POST /api/scorer/season-activation/{season_id}`
   (`app/routes/season_activation.py`, page at
   `/scorer/season-activation/{season_id}`) is now the browser workflow
   every Scorer or Administrator uses: a read-only readiness preview
   (season entries, player pool/completed squads, ordinary competition
   structure, fixture-draw freeze, preseason draft finalisation and trade-window closure) followed
   by an explicit, reason-required confirmation, reusing the existing
   `season.lifecycle.changed` audit event and completed-season write
   fence -- see [`season-activation.md`](season-activation.md). Available
   only to Scorer/Administrator authority (narrower than Season setup's
   Scorer/Secretary/Administrator), with the readiness link surfaced from
   the Season Centre, Season setup's own nav bar and an additive
   Administrator Dashboard card. **Evidence level:** automated-test-proven
   (`tests/test_season_activation.py`, `tests/test_season_activation_api.py`,
   `tests/test_season_activation_postgresql.py` for the readiness locking);
   not yet staging/production-rehearsed.
4. **Season completion/archival production path -- resolved by issue
   #240.** `GET`/`POST /api/scorer/season-completion/{season_id}`
   (`app/routes/season_completion.py`, page at
   `/scorer/season-completion/{season_id}`) is now the browser workflow
   every Scorer or Administrator uses to complete a season: a read-only
   readiness preview followed by an explicit, reason-required
   confirmation, calling `app.season_completion.preview_complete_season`/
   `complete_season` unchanged -- the same six-step atomic transaction,
   Premiership/Wooden-Spoon recording and permanent completed-season write
   fence issue #195 already proved. `GET /api/scorer/season-completion/
   {season_id}/archival-verification` calls `app.season_archival.verify_
   season_completed_for_archival` unchanged and is read-only/production-
   safe by construction. Neither the 2026-replay-only
   `scripts/season_completion_2026.py` nor
   `scripts/season_archival_checkpoint_2026.py` was touched -- both retain
   their own `BBBFFL_ENVIRONMENT=production` refusal exactly as before;
   this issue adds a separate, production-safe browser path to the same
   underlying domain functions rather than weakening either script's
   guard. See [`season-completion.md`](season-completion.md).

   **Evidence level:** automated-test-proven
   (`tests/test_season_completion.py`, `tests/test_season_completion_api.py`).
   It is **not** replay-proven or staging-proven: it has not been
   exercised against a real production deployment or a non-technical
   operator. For the rare case where a wooden-spoon tie is introduced by a
   post-bracket-freeze result correction, it still surfaces cleanly (409,
   atomic, no partial write) through the browser rather than as an
   unhandled error, and now has a recovery path via item 7's audited
   ladder-tie-ruling mechanism (resolved by issue #241) rather than being a
   permanent block.
5. **Issue #233 -- two Scorer UX cleanups found during the Round 10 ->
   mid-season regression:**
   - remove the obsolete `Legacy Grand Final admin` link from the ordinary
     Scorer Round Centre;
   - give the post-trigger-round mid-season handoff cue proper prominence
     in the Scorer's Next-safe-action/attention-queue area, instead of
     leaving a future ordinary-round preflight as the most visible action.

   **Resolved by the #233 implementation PR** (this item is retained for
   the record). The underlying
   Round 10 -> mid-season-draft workflow itself already passed regression
   (Stage D); #233 is release tidy-up, not a re-open of that finding.
6. **Non-technical Scorer staging/beta rehearsal**, and **a production
   backup/restore runbook rehearsal against a real deployment** (as
   distinct from the replay's repeatedly-proven checkpoint mechanism).
7. **Any exact ladder equality, anywhere on the ladder, once Finals must
   seed from the mathematical ladder (no 2026-style historical snapshot),
   or at last place for the Wooden Spoon -- resolved by issue #241.**
   `app.ladder_tie_ruling` (migration `0037_ladder_tie_ruling`) is now the
   supported, audited recovery path: `_resolve_seed`/`_resolve_ladder_seed`
   and `app.season_awards`'s wooden-spoon derivation still fail closed on
   an exact tie exactly as before, but now consult a persisted ruling
   first, and the Scorer Operations "Ladder tie ruling" page
   (`/scorer/ladder-tie-ruling/{season_id}`) is the discoverable browser
   workflow for recording one -- no CLI or database access is needed. See
   [`ladder-progression.md`](ladder-progression.md#unresolved-exact-ties-audited-manual-ruling-issue-241)
   and the "Ladder and results" row above.

   **Evidence level:** automated-test-proven (`tests/test_ladder_tie_ruling.py`,
   `tests/test_ladder_tie_ruling_api.py`, plus new cases alongside the
   existing tie coverage in `tests/test_finals.py`/`tests/test_finals_seeding.py`/
   `tests/test_season_completion.py`/`tests/test_scorer_dashboard.py`). It
   is **not** replay-proven or staging-proven: no 2026 replay ladder
   happened to land on an exact tie, so a real Scorer has never recorded a
   ruling through the browser workflow against production-shaped data.
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
    backups with an accepted RPO/RTO, and a rollback runbook -- addressed
    by issue #243.** `compose.production.yaml` and `deploy/production/`
    now give a reproducible four-service topology (app, PostgreSQL, a
    Caddy TLS reverse proxy, a scheduled-backup service), a separate
    `GET /health/ready` dependency-readiness check alongside the unchanged
    `GET /health` liveness check, structured `CRITICAL`/`WARNING`
    operational logging, a webhook-based alerting path, scheduled
    PostgreSQL backups with documented retention, a rehearsed restore
    procedure, and a rollback runbook covering both the application-only
    and database-affecting cases -- see
    [`production-operations.md`](production-operations.md) and
    [`evidence/production-operations-rehearsal-2026-09-27.md`](evidence/production-operations-rehearsal-2026-09-27.md).
    Steve has accepted the RPO (24h)/RTO (4h) targets as the production
    recovery targets for v0.1 (JustPlausible/BBBFFL_Scoring#249, 27
    September 2026). **What remains:** this was rehearsed end-to-end in a
    disposable sandbox environment, not against Steve's real production
    host, real DNS/TLS, or a non-technical operator; and the
    alert-webhook destination and host cron/systemd-timer entry for the
    readiness watchdog are still his to configure on the real host -- see
    `production-operations.md`'s "What remains environment-specific" for
    the complete list. Notification
    delivery and an incident/fallback runbook (roadmap package 40) remain a
    separate, related gap classified as deferred/v0.2 in the matrix above,
    since they do not block running an individual round.
11. **Live `afl-api` deployment validation -- substantially validated by
    issue #244 (2026-09-27); one thing remains open.** The entire 2026
    replay used `ReplayAflDataSource`, never the live client, and CI is
    hermetic, so no earlier evidence in this document validated that the
    actual production `afl-api` deployment is reachable or
    contract-compatible. That gap is now closed:
    `docs/afl-api-v1-contract.md`'s
    ["Live validation status"](afl-api-v1-contract.md#live-validation-status)
    section records a positive run of `scripts/afl_contract_diagnostic.py`
    against the configured deployment -- every endpoint this document's
    contract classifies "required now" returned real, contract-compatible
    2026-season data, and `/openapi.json` was compared with no incompatible
    difference. Match-lifecycle `status` and player-stat
    `lifecycle.finality` were confirmed live as genuinely independent
    signals; the full four-state vocabulary, including `POSTGAME`
    specifically not collapsing into `CONCLUDED`, remains source/test-
    confirmed rather than live-observed, since every match encountered in
    this run was already `CONCLUDED`.

    **Season 85 (2026) player-stats completeness was genuinely time-bound,
    and is now resolved.** A first validation pass (~11:47 UTC) found
    season 85's round/match structure fully present (30 rounds, 218
    matches) but three matches -- the two Preliminary Finals and the Grand
    Final -- reporting `lifecycle.finality="not_available"` with zero
    player rows, correctly blocking packages 08/32 at the time. The
    operator reported the upstream provider had backfilled those three
    matches from the authoritative CFS source; a second validation pass
    (~12:51 UTC) confirmed all three now report `finality="final"` with 46
    player rows each, and a full sweep of all 218 matches in season 85
    (every match checked individually, not a sample) found zero remaining
    incomplete matches. **This no longer blocks packages 08/32.**

    **Credential validation was also genuinely open, and is now closed.**
    The validating session's own network path auto-authenticated every
    request regardless of the key sent, so its diagnostic run could not
    distinguish "the deployment accepts any key" from "a real key is
    enforced and already authenticated" -- its missing/invalid-key ->
    `401` checks could not be independently exercised from that path. The
    operator then independently ran the equivalent three checks directly
    from the BBBFFL production Docker host, on a network path that does
    not auto-authenticate: no key -> `401`, an invalid key -> `401`, the
    real configured key -> `200`. This is exactly the follow-up this
    document called for and confirms the deployment enforces its
    configured credential.

    **What genuinely remains open, and is not a BBBFFL contract defect:**
    - No 2027 season resource has been published upstream yet (an expected
      off-season state the day after the 2026 Grand Final). This is the
      actual blocker for exercising Season setup's live player-pool read
      against a real 2027 season -- **not** the absence of an `is_current`
      flag: `app/season_setup.py` never calls
      `AflApiClient.get_current_season()`; it lists every season via
      `get_seasons()` and lets the operator select an explicit
      `afl_season_id`. (`get_current_season()` is used only by the legacy
      Grand Final/SuperScore prototype's `app/service.py`, which this item
      does not concern.) An earlier draft of this item incorrectly named
      `get_current_season()`/`is_current` as the Season setup dependency;
      corrected here.

    This remaining item does not block v0.1 by itself, and is not a
    contract incompatibility -- what remains is upstream timing for the
    2027 season's publication.

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
- It does not claim the issue #237 Season setup workflow is replay- or
  staging-proven; it is automated-test-proven plus one clean disposable-
  database acceptance run. (`scripts/bootstrap_2026_first_half.py`'s
  production guard, missing when this document was first written, was
  added by #237 and is covered by an automated test.)
- It does not claim issue #243's production deployment/readiness/backup/
  rollback baseline was rehearsed against Steve's real production host,
  real DNS/TLS, or a non-technical operator -- it was rehearsed end-to-end
  in a disposable sandbox environment only (see
  `evidence/production-operations-rehearsal-2026-09-27.md`). Steve has
  explicitly accepted the RPO (24h)/RTO (4h) targets themselves
  (JustPlausible/BBBFFL_Scoring#249, 27 September 2026).
- It does not claim issue #243 validates the real deployed `afl-api`
  contract. `GET /health/ready`'s afl-api check proves the *mechanism*
  works (it correctly reported the deliberately unreachable example
  afl-api endpoint as down during the #243 rehearsal); it does not, and
  did not, prove the real production `afl-api` deployment is reachable or
  contract-compatible. That was a separate item (11, above), substantially
  validated by issue #244 -- see that item for what genuinely still
  remains open (only: no 2027 season published upstream yet). Season 85's
  player-stats completeness, initially incomplete for three matches, was
  confirmed season-wide after an upstream backfill, and credential
  validation, initially unconfirmed from the validating session's
  auto-authenticating network path, was independently confirmed by the
  operator from an unproxied network path -- see item 11.
- It does not treat any v0.2/deferred item as blocking `v0.1.0`.
