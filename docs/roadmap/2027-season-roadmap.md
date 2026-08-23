# BBBFFL 2027 full-season engineering roadmap

**Status:** investigation baseline, 23 August 2026<br>
**Scope:** sequencing and validation, not implementation<br>
**Primary product authority:** [`../plans/2027-season-model.md`](../plans/2027-season-model.md), superseded where explicit by [`../plans/2027-season-decisions.md`](../plans/2027-season-decisions.md)

## 1. Executive summary

The repository has a good **scoring vertical slice**, not yet a season system. The FastAPI prototype already demonstrates the hardest narrow boundary—`coach selection + AFL facts + scorer rulings -> BBBFFL result`—with one scoring implementation, an `afl-api` adapter, scorer-controlled DNP/Interchange/overrides, explicit finalisation, frozen snapshots, public views, and an opt-in ten-entry SuperScore view. Its deterministic tests are substantial and should be preserved.

The prototype's JSON teams, single-purpose SQLite tables, shared admin token, and in-process polling are intentionally weekend-scale. There is no persistent season/coach/squad/draft/fixture/lineup/ladder/finals model, coach workflow, formal migration framework, audit event history, deployment topology, backup procedure, or season replay harness. The sensible path is therefore **evolution around the tested scoring core**, not a rewrite and not expansion of the prototype tables into an accidental schema.

This roadmap proposes **seven milestones and 40 candidate work packages**. The critical path establishes a season-aware persistence/audit foundation, validates the versioned AFL contract and historical access, models participants and ownership, then implements fixture/results, weekly lineups/lockouts, scorer finalisation, and ladder progression. Preseason draft, mid-season operations, finals, SuperScore selection streams, and production controls build on those foundations.

The 2026 replay is a progressive acceptance gate:

1. a first useful one-round replay starts after work packages **01–11, 16–18, 21–29, and 32** (coach authentication packages 19–20 are not needed for a service-level replay);
2. the lockout/bye edge-case checkpoint follows after **33**, and the mid-season checkpoint follows after **30–31 and 34**;
3. a complete 2026 season replay becomes feasible after **30–31, 35, and 37–38**, and is executed/triaged by work package **36**, with historical AFL data supplied through the public `afl-api` contract.

Replay evidence must label each datum as **known fact, reconstructable behaviour, synthetic scenario, or unresolved scorer input**. Passing replay does not turn synthetic assumptions into league rules.

### Evidence and confidence convention

Material claims below carry one or more evidence tags:

- **[PLAN]** authoritative season model or confirmed decisions;
- **[DECISIONS]** a rule explicitly confirmed in the 2027 decisions/coach outcomes;
- **[APP]** current FastAPI implementation;
- **[TEST]** current automated tests;
- **[LEGACY]** archived GAS or forensic review;
- **[2026]** scorer-workbook findings;
- **[GIT]** repository history/workflow configuration;
- **[INFERENCE]** engineering judgement, to revisit as implementation teaches us more.

## 2. Current-state assessment and investigation basis

### Material reviewed

The investigation read all five required planning documents, the root and application READMEs, every current Python application module and test, container/environment configuration, CI workflow, the archived GAS README and forensic review, and targeted legacy fixture/ladder/submission/result code. The workbook itself is not stored in this repository; `2026-workbook-findings.md` is therefore the repository's evidence report, not a fresh workbook extraction. [2026]

Git history contains merged application PRs #2–#11, planning PR #12, and legacy archival PR #14; the current branch also contains the forensic review commit. GitHub Issues, labels, milestones, and Projects could not be queried from this environment because the checkout has no configured remote, GitHub CLI is unauthenticated, and outbound GitHub API access returned 403. Consequently, this document does **not** assert that no backlog exists. Candidate issues must be deduplicated against GitHub before creation. [GIT]

### Capability classification

| Area | Evidence-based current state | Classification |
|---|---|---|
| Application architecture | One FastAPI process; route, service, pure scoring, presentation, AFL adapter, JSON config, and SQLite repository modules are already separated. [APP] | **Reusable foundation**, with application/service boundaries to formalise as domains grow |
| Persistence | SQLite stores only DNP, Interchange assignment, overrides and one finalised snapshot, keyed by `competition_key`; JSON stores declared teams. [APP][TEST] | **Usable but requires generalisation**; prototype schema should not become the full season model by accretion |
| Schema management | Startup DDL and bespoke compatibility migration, with migration regression tests; no numbered migration tool or schema lifecycle. [APP][TEST] | **Prototype-only for future schema evolution** |
| AFL integration | Thin synchronous `/api/v1` client for seasons, rounds, matches, player identity and match player stats; match statuses normalised and POSTGAME kept distinct. [APP][TEST] | **Reusable seam**, but needs timeout/retry/cache/contract/history validation |
| Scoring engine | Pure canonical formulas for Forward, Midfield, Tackler and Ruck, reused by head-to-head and SuperScore. [APP][TEST][PLAN] | **Reusable foundation**; make rules season-versioned without duplicating calculations |
| Head-to-head / Grand Final | Two configured entries use generic scoring orchestration, running state, margin/leader and scorer sign-off. [APP][TEST] | **Usable but requires generalisation** into persisted matchup/round/result entities |
| SuperScore | Opt-in N-entry ranking reuses scoring and isolated decisions; one configured round at a time, no selection stream/carry-forward/four-week history. [APP][TEST] | **Usable but requires generalisation** |
| Match lifecycle | UPCOMING/LIVE/POSTGAME/CONCLUDED map to yet-to-play/live/postgame/completed; only concluded permits sign-off. [APP][TEST] | **Reusable foundation**, pending upstream correction/history semantics validation |
| DNP | Scorer explicitly marks slots; zero stats do not auto-DNP; original player remains visible. [APP][TEST][PLAN] | **Reusable foundation**, with recommendation evidence and audit still missing |
| Interchange | Explicit scorer assignment, intentional vacancy supported, potential scores shown, one contribution only. [APP][TEST] | **Usable but requires generalisation**: automatically recommend the confirmed highest eligible score |
| Overrides/finalisation | Admin can override position score and freeze snapshot; mutations then lock; final snapshot survives AFL outage. [APP][TEST] | **Reusable pattern**, but needs actor/reason audit and authorised reopen/correction workflow |
| Scorer/admin UI | Server-rendered controls for DNP, Interchange, score override and finalisation. [APP] | **Usable but requires generalisation** to five matches, queues, permissions and audit |
| Public UI | Polling Grand Final scoreboard and SuperScore leaderboard expose only intended scoring detail. [APP][TEST] | **Usable but requires generalisation** into Round Centre, ladder and finals; mobile smoke coverage missing |
| Team/config storage | Checked-in JSON with placeholder IDs supplies one Grand Final and optional one-round SuperScore. [APP] | **Prototype-only / replace** with persisted squads and submitted lineups |
| Authentication | Optional shared `X-Admin-Token`; if unset, writes are open; no coach identity/session/CSRF model. [APP][TEST] | **Prototype-only / replace before public deployment** |
| Audit/history | Current values and final snapshots exist, but mutations overwrite state and actor identity is absent. [APP] | **Missing** as an auditable season capability |
| Tests | Broad deterministic scoring/service/API/status/DNP/Interchange/SuperScore/isolation/migration coverage with fake AFL clients. [TEST] | **Reusable foundation**; gaps are season domains, auth, browser/UI, real contract evidence and replay |
| CI | GitHub Actions runs Python 3.11 pytest and Docker build on PR/main. [GIT] | **Reusable but requires strengthening** (lint/type, migration, security/health/replay tiers) |
| Runtime | Python 3.11 slim image, Uvicorn, environment config, persistent volume, HTTP healthcheck and standard logging. [APP] | **Usable but requires production operations** |
| Health/observability | `/health` only reports process availability; client logs request failures. No readiness/dependency probes, metrics, alerting or correlation IDs. [APP] | **Missing** operationally |
| Fixture, ladder, draft, ownership, weekly selection, finals bracket, history import, notifications | Domain intent and legacy evidence exist; modern implementations do not. [PLAN][LEGACY][2026] | **Missing** |

### Current-state verdict

Keep the scoring functions, AFL boundary, lifecycle distinction, scorer-decision pattern, final snapshot concept, public/admin separation, and deterministic fake-client tests. Generalise their inputs and persistence behind explicit season/round/competition identities. Replace configuration files and shared-token administration only when their persisted successors exist; do not pause feature delivery for a framework rewrite. [APP][TEST][INFERENCE]

## 3. Architectural principles and boundaries

1. **Keep AFL facts upstream.** BBBFFL uses only the public, versioned `afl-api` consumer contract. It never calls Champion Data/CFS, edits AFL facts, fuzzy-matches provider identities as a normal path, or becomes a second AFL system of record. [PLAN][LEGACY]
2. **Keep fantasy derivation explicit.** `coach selection + AFL facts + scorer rulings -> BBBFFL result`; each input and its version/source must be inspectable. [PLAN][APP]
3. **Make season and competition context first-class.** Rules, squads, fixture mapping, ordinary matches, finals and SuperScore must not rely on magic keys such as `grand_final` or the current AFL round. [PLAN][APP]
4. **Retain one scoring core.** Add a season rules input around the tested formulas; do not fork live/final/SuperScore implementations as legacy GAS did. [APP][TEST][LEGACY]
5. **Model facts and events, derive read views.** Persist selections, ownership transactions, rulings and final results; derive scoreboards, ladder and records. Finalisation freezes an official version, while an authorised correction creates traceable history rather than editing invisibly. [PLAN][2026][INFERENCE]
6. **Use a modular monolith first.** Approximately ten coaches do not justify microservices, event streaming or elaborate realtime infrastructure. FastAPI plus a relational database, server-rendered/mobile-first pages, and bounded background jobs are sufficient. [PLAN][INFERENCE]
7. **Use scorer authority without weakening invariants.** The scorer can resolve exceptional league cases, but uniqueness, squad size, locking and finalisation transitions remain transactionally protected; overrides require actor, time and reason. [PLAN]
8. **Separate public, submitted and private data.** Public competition facts and submitted teams are readable without login; private drafts/contact details and all writes require appropriate identity. [PLAN]
9. **Replay deterministically.** Store controlled AFL response fixtures/evidence and a replay clock. Live integration diagnostics must be separate from hermetic regression tests. [PLAN][TEST][INFERENCE]
10. **Prefer incremental replacement.** Preserve prototype endpoints/read models while persisted season capabilities arrive, then retire JSON and magic competition keys through explicit migration. [INFERENCE]

## 4. Target 2027 capability map and gaps

| Domain | Minimum complete-season capability | Principal gap / evidence |
|---|---|---|
| Platform | Relational season schema, numbered migrations, configuration validation, audit events, CI, deploy, backups, readiness/observability | Only decision SQLite/startup DDL and process health exist. [APP] |
| Identity/access | Persistent coach; season entry/team name; password or emailed one-time sign-in; coach/scorer/admin roles; public spectator; private contacts; secure sessions | Only optional shared admin header exists. [APP][PLAN] |
| Season administration | Create/freeze season, version rules, ten licences, fees status if retained, lifecycle, AFL/BBBFFL round mapping, exceptional scorer configuration | Entirely missing. [PLAN] |
| Player pool | Canonical IDs, season membership/team, locally cached read model with provenance/expiry, provisional identity and audited reconciliation | Prototype resolves configured IDs one-by-one. [APP][PLAN] |
| Preseason draft | Random order, snake picks, configurable target, traded ownership, proxy entry, provisional players, immutable history and final squads | Missing; rules documented. [PLAN] |
| Squad transactions | Window states, balanced preseason trades, delistings, frozen reverse-ladder non-snake draft, traded picks, ownership audit | Missing. [PLAN] |
| Fixture/results/ladder | Fixture-number draw and exact verified rotation, AFL-round mapping, five matchups, official results, 4/2/0, PF/PA/%, equality escalation | Missing; exact rotation and formula evidence exist. [2026][PLAN] |
| Weekly selection | Nine slots, private draft, submit/publish/resubmit, squad/duplicate checks, per-match and main locks, proxy edit, carry-forward provenance, bye warning | Missing; legacy validation is evidence, not code to port. [PLAN][LEGACY] |
| Live operation | Five matches, canonical scoring, status updates, DNP recommendation/confirmation, best Interchange recommendation, overrides, sign-off/reopen | Narrow one-matchup foundation exists. [APP][TEST][PLAN] |
| Round Centre | All five matchups, player states, rulings, ladder context, mobile public view with privacy boundaries | Narrow Grand Final view exists. [APP] |
| Finals | Locked top five, four-week bracket, higher-seed tied-final recommendation, progression, final records | Only generic Grand Final scoring exists. [APP][PLAN][2026] |
| SuperScore | Separate SS1–SS4 selections/carry-forward, all ten, scores/standings/finalisation/history isolated from main records | One configured round's ranking/scoring exists. [APP][TEST][PLAN] |
| History/records | Preserve all 2027 operational facts; derive season/premier/spoon/records; import older seasons progressively with provenance and annotations | Final snapshots only; workbook evidence identifies sources. [APP][2026] |
| Notifications | Pluggable reminders/warnings/admin events, non-production destinations for replay | Missing and supporting rather than core. [PLAN] |

## 5. Priority and work-estimate definitions

| Priority | Meaning |
|---|---|
| **P0 — Foundation/blocker** | Integrity, contract or delivery prerequisite; schedule first and protect with tests |
| **P1 — Required for 2027 core season** | Needed for the relevant live season phase; cannot safely remain manual for that phase |
| **P2 — Important operational/product capability** | Material reliability/usability; may have a documented temporary scorer procedure |
| **P3 — Enhancement / safe to defer** | Valuable after launch; must not displace integrity or replay work |

| Estimate | Meaning |
|---|---|
| **XS** | Isolated/small |
| **S** | Modest contained change |
| **M** | Meaningful feature or cross-cutting change |
| **L** | Substantial domain capability, likely several reviewable PRs |
| **XL** | Epic only; decompose before creating implementation issues |

“Blocks replay” means blocks at least one stated replay checkpoint, not necessarily checkpoint 1. “Defer?” means safe after launch; **No** means required by the season phase shown in dependencies.

## 6. Proposed milestones and dependency gates

| Milestone | Outcome / exit gate | Parallel work |
|---|---|---|
| **A — Production-quality foundation** | 01–08: versioned schema/audit/config, resilient validated AFL seam, strengthened CI. Existing prototype remains green. | 04/05 AFL work can run beside 02/03 schema design after identifiers are agreed. |
| **B — Season setup and preseason** | 09–15: 2027 season, coaches/entries, player pool, draft and opening ownership can be operated and audited. | Identity UI and player-pool ingestion can proceed beside draft engine after 09–11. |
| **C — Regular season playable** | 16–29: verified fixture, selections/lockouts, five live matchups, rulings, finalisation and ladder make Round 1 operable. | Fixture/result work and lineup UI can proceed in parallel after common schema; scoring generalisation can proceed with both. |
| **R — 2026 replay validation gate** | 32–36: progressive checkpoints culminate in a deterministic complete-season report; findings become issues/reprioritisation, not hidden fixture edits. | Harness/fixture curation starts during A; checkpoints execute as C/D/E capabilities land. |
| **D — Mid-season operations** | 30–31: post-Round-9 priority, delist/trade/draft windows restore valid squads with history. | Can follow ownership/window model while regular-season UI is refined. |
| **E — Finals and SuperScore** | 35 plus 37–38: top-five bracket and four independent SuperScore streams operate through season finalisation. | SuperScore stream work reuses scoring while bracket is developed. |
| **F — 2027 operational readiness** | 39–40 plus relevant P2 work: secure deployment, backup/restore drill, monitoring, runbooks, rehearsal and go/no-go sign-off. | Deployment/runbooks begin early; final rehearsal waits for E and complete replay. |

Milestone R is deliberately a cross-cutting release gate rather than a last-minute testing phase. Full replay completion is an exit criterion for F even though replay work begins during A.

### Critical paths

- **Preseason draft:** 01 → 02 → 09 → 10 → 11 → 12 → 13 → 14 → 15.
- **Playable Round 1:** 01 → 02 → 09 → 10 → 11 → 16 → 17 → 18 → 21 → 22 → 23 → 24; then **19 → 20** (which can be built in parallel with 16–18 and 21–24) → 25 → 26 → 27 → 28 → 29. Authentication and authorisation are on this path because the coach selection page cannot be operated safely without them.
- **Mid-season draft:** playable-round result/final ladder through Round 9 → 30 → 31.
- **Finals:** final regular ladder → 35 → 27–28 reused → 37 → season finalisation in 40.
- **Production readiness:** 01–08 → 19–20 → core phase features → 39 → complete replay (36) → 40.

The top immediate critical-path packages are: **01 schema/migrations, 02 audit, 04 contract validation, 09 season model, 10 coach/entry identity, 11 player pool/ownership, 16 fixture mapping, 21 lineup persistence, 23 staged locks, and 27–29 scoring/finalisation/ladder**.

## 7. Ordered candidate Issue backlog

Each row is a candidate work package, not an automatically approved GitHub Issue. Deduplicate against the live tracker. An `XL` row is an epic whose listed children should be separate issues.

| # | Working title | Outcome | Pri / size | Dependencies and sequencing reason | Tests / validation and acceptance signal | Replay | 2027 | Defer? |
|---:|---|---|---|---|---|---|---|---|
| 01 | Adopt versioned relational migrations | Choose supported production DB (PostgreSQL recommended; SQLite permitted for hermetic tests), baseline current decision data, add ordered upgrade/downgrade policy. | P0 / L | First: every persistent domain depends on stable migration ownership. | Fresh/upgrade/rollback and idempotence tests; existing decision snapshot migrates losslessly in CI. | Yes | Yes | No |
| 02 | Define append-only audit event boundary | Actor, role, time, reason, entity/version and before/after references for admin/proxy/transaction/ruling changes. | P0 / M | After 01; before privileged season workflows so audit is not retrofitted. | Repository/invariant tests; DNP/override change produces attributable immutable events. | Yes | Yes | No |
| 03 | Separate domain services and repositories without rewriting scoring | Establish season, roster, selection, competition and scoring module boundaries; preserve routes/read models during transition. | P0 / M | Alongside 01–02; prevents route handlers and prototype DB from owning new rules. | Architecture/import smoke tests and existing suite; one persisted use case crosses explicit interfaces. | Yes | Yes | No |
| 04 | Validate and pin `afl-api` v1 consumer contract | Record required endpoints/status semantics/auth/version compatibility and controlled response fixtures. | P0 / M | Early external risk; informs player, lockout and replay designs. | Contract parser tests plus opt-in integration diagnostic; compatibility report approved. | Yes | Yes | No |
| 05 | Add resilient AFL client cache and diagnostics | Bounded retry/backoff, timeouts, request IDs, cache freshness/provenance and stale/unavailable presentation; no second authority. | P0 / M | After 04; required before live scoring expands. | Failure/timeout/stale/cache tests; outage never corrupts/finalises results and diagnostics identify dependency. | Yes | Yes | No |
| 06 | Make configuration explicit and fail-safe | Validate secrets, DB URL, public base URL, AFL contract version, replay mode and environment separation at startup. | P0 / S | With foundation; removes permissive prototype defaults from production. | Settings tests; production refuses missing admin/session secrets and invalid endpoints. | No | Yes | No |
| 07 | Strengthen CI quality gates | Add format/lint/type (incrementally), migration tests, dependency/security check policy and separate hermetic/integration jobs. | P0 / M | Early feedback for all later packages; retain pytest and image build. | Required checks pass on clean checkout; integration job clearly optional/credentialed. | No | Yes | No |
| 08 | Curate deterministic AFL evidence fixtures | Sanitised/versioned 2026 player/match/stats/status fixtures with capture metadata and refresh procedure. | P0 / M | After 04; enables replay without network/non-repeatable corrections. | Schema validation and fixture provenance check; tests load fixtures independently of live API. | Yes | Yes | No |
| 09 | Introduce season-aware competition schema | Season, lifecycle, versioned rules, competition streams and AFL/BBBFFL round mappings; no magic current round. | P0 / L | On 01–03; parent identity for all season features. | Model/migration/invariant tests; 2026 replay and 2027 season coexist without key collisions. | Yes | Yes | No |
| 10 | Implement coach and season-entry identity | Persistent private coach/contact identity separated from season licence and public team name/history. | P0 / M | After 09; draft, auth and records reference entries. | Uniqueness/privacy/history tests; rename/replacement preserves correct identities. | Yes | Yes | No |
| 11 | Implement season player pool and ownership ledger | Canonical AFL identity references, season eligibility snapshot/read cache, exclusive time-bounded ownership and squad-size validation. | P0 / L | After 09, validated by 04; prerequisite to draft and lineups. | Transaction/concurrency/invariant tests; no player has overlapping owners in a season. | Yes | Yes | No |
| 12 | Support provisional AFL players and reconciliation | Admin creates clearly provisional identity and later links canonical ID without rewriting draft/ownership history. | P1 / M | After 11; required before real draft can tolerate upstream gaps. | Collision/reconciliation/audit tests; history retains original pick and canonical link. | Maybe | Yes | No |
| 13 | Implement draft order, picks and pick ownership | Random draw recording, derived snake sequence, adjustable target, picks independently tradeable and auditable. | P1 / L | After 09–12; establishes selection ledger before UI. | Property tests across squad sizes; consecutive turn picks and traded owner execute correctly. | Maybe | Yes | No |
| 14 | Build scorer-operated preseason draft workflow | Live board, proxy picks, validation, undo/correction with audit, pause/resume and explicit finalise. | P1 / L | After 13; operational surface can initially be scorer-first. | API/UI workflow and concurrency tests; full synthetic ten-team draft resumes and finalises. | No | Yes | No |
| 15 | Finalise opening squads and preseason trades | Balanced multi-club player trades/window close; freeze opening ownership at first official AFL match. | P1 / M | After 11 and 14; closes preseason state used by Round 1. | Atomicity/size/window/audit tests; opening squads all valid and closed edits rejected. | Yes | Yes | No |
| 16 | Persist fixture-number draw and historical rotation | Store independent draw; generate exact nine-round pattern/reversals/repeats from workbook evidence. | P0 / M | After 09–10; result/lineup work needs matchups. | Golden 2026 pairing tests and “each pair once in 1–9”; scorer evidence sign-off. | Yes | Yes | No |
| 17 | Model exceptional BBBFFL-to-AFL round mapping | Explicit mappings, finals/SuperScore streams, byes/opening/unusual structures; never assume round numbers align. | P0 / M | After 09 and 04; fixture, locks and replay clocks depend on it. | Mapping/state tests for ordinary and unusual 2026 rounds; no ambiguous mapping can open. | Yes | Yes | No |
| 18 | Persist round, matchup and result lifecycle | Upcoming/open/live/review/final/corrected states, five matchups and immutable official result versions. | P0 / L | After 09, 16–17; foundation for scoring and ladder. | Transition/concurrency/migration tests; invalid transitions and partial finalisation cannot publish. | Yes | Yes | No |
| 19 | Implement simple coach authentication and sessions | Small-user login (password or emailed one-time link chosen by implementation), secure cookie/session rotation/logout/recovery. | P1 / L | After 10 and before coach writes; can parallel fixture engine. | Auth/session/CSRF/rate-limit tests; coach can access own private work only. | No | Yes | No |
| 20 | Enforce role and public/privacy policy | Coach own-team writes, scorer/admin delegation, spectator reads; protect contacts/drafts/audit notes. | P0 / M | After 19 and 02; required before public deployment. | Permission matrix/API tests; anonymous/private cross-team access is denied. | No | Yes | No |
| 21 | Persist weekly lineup drafts and submissions | Nine slots, private saved versions, submit/publish/resubmit provenance and normal vs SuperScore stream identity. | P0 / L | After 11, 17–18; ownership and round context are prerequisites. | Schema/API/invariant tests; draft stays private, submitted version visible, duplicate/unowned players rejected. | Yes | Yes | No |
| 22 | Implement carry-forward and scorer proxy entry | Previous relevant stream copied without optimisation, explicit Round 1/SS1 scorer confirmation, source metadata and proxy audit. | P1 / M | After 21 and 02; handles non-submission safely. | Golden transition tests; copied lineup is exact and never presented as coach-submitted. | Yes | Yes | No |
| 23 | Implement staged AFL-match lockouts | Scorer-configured early match set/main trigger, per-player/slot locks and no later additions from started clubs. | P0 / L | After 04, 17, 21; uses authoritative start/state facts and persisted versions. | Fake-clock state/property tests covering multiple early matches/resubmission; forbidden edits are atomic. | Yes | Yes | No |
| 24 | Add lineup validation and bye/availability warnings | Ownership, nine slots, uniqueness hard rules; ordinary bye warnings without blocking or DNP activation. | P1 / M | After 11, 21, 04; before coach UI. | Domain/API tests for duplicates, unowned, byes and incomplete drafts; submitted valid lineup accepted. | Yes | Yes | No |
| 25 | Build mobile-first coach selection page | Owned squad, private save, submit/status, lock indicators, visible submitted teams, accessible small-screen workflow. | P1 / L | After 19–24 APIs; UI must reflect real transitions rather than mock state. | Browser/mobile viewport smoke and usability rehearsal; ten coaches can complete representative flows. | Yes | Yes | No |
| 26 | Generalise scoring to season rules and five matchups | Feed versioned scoring rules and persisted selections into the existing pure core; preserve calculated/effective separation. | P0 / L | After 09, 18, 21; before round centre/finalisation. | Existing tests plus season-rule and five-match golden tests; same input yields same live/final calculation. | Yes | Yes | No |
| 27 | Implement DNP evidence and best-Interchange recommendation | Surface participation evidence; recommend—not assert—DNP and highest-scoring eligible target including intentional vacancy. | P1 / L | After 04–05, 21, 26; applies confirmed coach priority. | DNP/bye/zero-stat/multiple-target tests; recommendation never mutates official score before scorer action. | Yes | Yes | No |
| 28 | Build scorer round review, sign-off and correction workflow | Queue five matchups, rulings/reasons, manual score override, atomic round publish, authorised reopen/new result version. | P0 / L | After 02, 18, 26–27; official results gate ladder. | Role/audit/concurrency/failure tests; sign-off freezes exact inputs and correction preserves prior official version. | Yes | Yes | No |
| 29 | Implement deterministic ladder progression | 4/2/0, W/D/L, PF/PA/percentage/PPG, order and exact-equality escalation from final results only. | P0 / M | After 18 and 28; mid-season/finals depend on official ladder. | Round-by-round 2026 golden/property tests; recomputation matches workbook or records documented discrepancy. | Yes | Yes | No |
| 30 | Implement mid-season window and delistings | Freeze reverse post-R9 order, open/close delist/trade periods, allow vacancies while window open. | P1 / M | After 11, 15, 29; priority requires official ladder. | Window/state/audit tests; frozen order cannot drift after later corrections without explicit ruling. | Yes | Yes | No |
| 31 | Implement non-snake mid-season draft and traded picks | Repeating priority, skip full clubs, variable vacancy count, pick ownership trades and final squad validation. | P1 / L | After 30 and 12–13 patterns; required before second half roster state. | Property/workflow tests; synthetic full-squad restoration and 2026 evidence comparison. | Yes | Yes | No |
| 32 | Build one-round 2026 replay harness | Seed labelled evidence, control time/statuses, run setup→selection→locks→score→sign-off→ladder, emit machine/readable diagnostics. | P0 / L | Starts with 08; useful after 16–17, 21–29 minimum slice exists. | Idempotent CLI/test on clean DB; report distinguishes all four evidence classes and expected deltas. | Yes | No* | No |
| 33 | Replay early-lockout, bye and non-submission rounds | Exercise multiple early matches, ordinary byes, partial/missing submission and carry-forward without silently resolving ambiguity. | P0 / M | After 22–24 and 32. | Golden checkpoint report; scorer questions explicitly flagged, reruns deterministic. | Yes | No* | No |
| 34 | Replay post-Round-9 transition | Reconstruct known ownership/delist/draft facts; insert labelled synthetic scenarios where evidence is absent. | P1 / M | After 30–32. | Ownership ledger reconciles at pre/post checkpoints; unknowns remain an input manifest. | Yes | No* | No |
| 35 | Implement top-five finals bracket and progression | Lock seeds, four-week structure, higher-seed tie recommendation, scorer-confirmed progression and Grand Final result. | P1 / L | After final regular ladder, 18 and 28; reuse matchup scoring. | Bracket state/property tests plus 2026 finals golden checkpoint. | Yes | Yes | No |
| 36 | Run complete 2026 season replay and triage | Execute all rounds, mid-season, finals, SS1–SS4 and finalisation; publish discrepancy/evidence/risk report and convert findings to reviewed issues. | P0 / L | After 31, 35, 37–38 and upstream historical data. | Repeat run produces identical official outputs; every difference classified/owned; scorer signs off acceptance limits. | Yes | Yes | No |
| 37 | Generalise SuperScore selection streams SS1–SS4 | Ten independent submitted lineups, SS1 ordinary-team fallback with scorer confirmation, SS2+ carry-forward and ownership validation. | P1 / L | After 21–24; preserve current scoring/ranking/isolation. | Stream/isolation/carry-forward/API tests and 2026 SuperScore golden rounds. | Yes | Yes | No |
| 38 | Persist SuperScore standings, prizes and separate records context | Official per-round results/history, ties/prize allocation configuration, exclusion from normal records. | P1 / M | After 37 and 28 patterns; completes finals participation/history. | Four-round finalisation and record-scope tests; public history matches signed results. | Yes | Yes | No |
| 39 | Production deployment, observability and recovery | Reproducible deploy, TLS/reverse proxy, readiness, structured logs/alerts, scheduled backup, restore and rollback drills. | P0 / L | Foundation can start early; final drill needs full schema and auth. | Container health/readiness, smoke test, measured restore rehearsal and documented RPO/RTO accepted by operator. | No | Yes | No |
| 40 | Operational runbooks, notification essentials and full rehearsal | Season/scorer procedures, incident/manual fallback, lockout/missing-team alerts via replaceable adapter, test destinations, end-to-end go/no-go rehearsal. | P1 / L | After core workflows, 19–20, 36, 39; final launch gate. | Staging rehearsal from season setup to finalisation; role checklist, alert delivery and rollback observed. | No | Yes | No |

`No*` in the 2027 column means replay harness code is not a sporting feature, but the **knowledge gained and complete replay gate are required risk controls before live 2027**.

### XL domains and suggested decomposition

No row above is intentionally XL. If tracker review combines them into epics, decompose rather than implement as one PR:

- **Weekly selection epic:** 21 persistence/API, 22 fallback/proxy, 23 lock engine, 24 validation/warnings, 25 coach UI.
- **Round operations epic:** 18 lifecycle, 26 calculation, 27 recommendations, 28 scorer finalisation/correction, 29 ladder.
- **Replay epic:** 08 evidence fixtures and 32–36 checkpoint issues.
- **Operational readiness epic:** 19–20 identity/security, 39 platform operations, 40 procedures/rehearsal.

## 8. 2026 mock-season replay strategy

### Replay is evidence, not data fabrication

Every replay input carries a classification and source reference:

| Class | Meaning | Treatment |
|---|---|---|
| **Known historical fact** | Direct workbook/submission/API evidence: fixture pairing, recorded lineup/score, final ladder entry, etc. | Golden assertion; discrepancy requires explanation or correction. |
| **Reconstructable behaviour** | Deterministically derived from known inputs and confirmed rule, such as a positional score. | Assert derivation and retain source/input hashes. |
| **Synthetic test scenario** | Deliberately constructed to cover missing or rare behaviour. | Clearly named; validates software only, never claims history. |
| **Unresolved scorer input** | Missing/ambiguous ruling such as a partial early submission. | Replay pauses/flags or uses an explicitly supplied scenario variant; never silently defaults. |

A repeatable CLI/job should create an isolated database, seed a season manifest, use a controllable clock and controlled AFL fixtures, execute domain commands through the same application services as production, and emit JSON plus a human discrepancy report. It must not send production messages or change live season state. [PLAN][INFERENCE]

### Progressive checkpoints

| Gate | Earliest roadmap point | Representative validation | Exit signal |
|---|---|---|---|
| **R0 — Scoring corpus** | 08 + existing core | Known player stat lines, POSTGAME→CONCLUDED, DNP, override and several Interchange target scores. | Hermetic golden tests preserve current prototype behaviour. |
| **R1 — One regular round** | 01–08, 09–11, 16–18, 21–29, 32 | Setup/import ownership, five pairings, selections, lock timeline, scoring, sign-off and one ladder update. | Idempotent report; all mismatches classified. This is the **first useful replay**. |
| **R2 — Lockout/bye edge case** | R1 + 22–24, 33 | Multiple early matches, ordinary club bye (not DNP), missing/carry-forward and partial submission variants. | Known results match; unresolved policy is surfaced to scorer. |
| **R3 — Mid-season transition** | R2 + 30–31, 34 | Frozen reverse ladder, delistings, trades/picks and non-snake replenishment. | Ownership balances before/after; gaps explicitly labelled. |
| **R4 — Finals/SuperScore** | R3 + 35, 37–38 | Seeding/progression/tied-final recommendation, SS1 fallback, SS2+ carry, Grand Final. | Finals and separate SuperScore outputs match known evidence. |
| **R5 — Complete season** | R4 + 36 and adequate upstream history | Every 2026 round/mapping, ladder evolution, ordinary/unusual rounds, mid-season, finals, four SuperScores and season finalisation. | Deterministic rerun; discrepancy register owned; scorer approves fitness conclusion. **Full replay feasible here.** |

Useful historical goldens include the exact nine-round pairing rotation and its repeats, weekly populated teams/scores, round-by-round ladder accumulation, real Interchange activations, bye-affected rounds, finals seeding/progression, four SuperScore rounds, Grand Final and record comparisons. [2026] Lockout timestamps, every DNP ruling, partial submissions and mid-season transaction detail may require WhatsApp/scorer evidence or synthetic variants; absence must remain visible. [PLAN][2026]

Replay failures should create or amend reviewed issues with fixture/source links, expected versus actual output, whether the defect is BBBFFL/upstream/evidence, and roadmap impact. Updating a golden is not an acceptable unexplained fix.

## 9. Testing strategy

Tests ship with each package, not in a later “testing phase.”

| Layer | Purpose and examples | Execution |
|---|---|---|
| Pure unit | Season-configured scoring, football presentation, ladder arithmetic, snake/non-snake ordering, fixture generation | Every PR; broad boundary/property cases |
| Domain invariants | Exclusive ownership, squad size/window rules, lineup uniqueness, visibility, state transitions, immutable final versions | Every PR against real repositories and transactions |
| Migration/database | Fresh database, every supported upgrade path, constraints/concurrency, backup restore | CI; production migration rehearsal before deploy |
| AFL contract | Parse captured v1 fixtures; status/finality, team/player identity, errors and corrections | Hermetic every PR; credentialed diagnostic scheduled/manual |
| Historical golden | 2026 fixture, scores, ladder, DNP/Interchange/finals/SuperScore evidence | Added progressively; immutable source metadata |
| Lockout/time | Fake clock; early set, main trigger, started-player immutability, resubmission and correction | Deterministic state-machine tests, never wall-clock sleeps |
| Auth/privacy | Session/CSRF, permission matrix, coach ownership, public response snapshots, private contact/draft exclusion | Every auth/API change |
| Service integration | Controlled AFL adapter + database + application services through complete round commands | Every PR for relevant domain |
| API/UI smoke | FastAPI request tests plus browser tests for coach/scorer/public critical flows and mobile viewports | CI smoke; richer staging rehearsal |
| Replay/regression | R0–R5 manifests with JSON/discrepancy output | Small checkpoint in CI; complete season scheduled/release gate |
| Container/operations | Image build, process/readiness, dependency-down behaviour, migration, backup/restore and rollback | CI plus staging/runbook drills |

Adopt the useful `afl-api` techniques without cloning its platform: reusable response fixtures, explicit evidence capture metadata, diagnostics that preserve raw/normalised context, and a bright line between live integration evidence and deterministic tests. The normal test suite must not depend on live AFL availability. [TEST][INFERENCE]

## 10. `afl-api` dependency assessment

This assessment is based on the adapter's documented/fixture-tested v1 endpoints, not inspection of the separate repository. “Probably” therefore requires contract or deployed-service validation in work package 04.

| Required BBBFFL fact/capability | Status | Evidence / required validation |
|---|---|---|
| Seasons and current round | **Already supported** | `/api/v1/seasons` parsed/tested; BBBFFL must stop relying on `is_current` for historical/configured rounds. [APP][TEST] |
| Season rounds, names, dates and bye list | **Probably supported; validate** | Adapter uses round ID/number but discards name/dates/byes currently; docstring records upstream fields. Validate semantics and historical retention. [APP] |
| Matches, AFL teams, start time and state | **Probably supported; validate** | IDs/status/team objects parsed; start time is documented but discarded. Validate timezone, rescheduling, state transitions and final corrections. [APP][TEST] |
| Canonical player identity and current AFL team | **Already supported for point lookup** | `GET /players/{id}` parsed/tested. Bulk season player population and season membership are not demonstrated. [APP][TEST] |
| Bulk season-aware player pool/team membership | **Upstream enhancement likely required** | Draft needs all listed players and as-of-season membership, not current-team point lookups. Confirm an uninspected public endpoint before filing. [PLAN][INFERENCE] |
| Historical rounds/matches/player stats for 2026 | **Probably supported; must validate as replay blocker** | Endpoints are ID-based, but retention/discovery/correction version semantics are unproven here. [APP][INFERENCE] |
| Player match stats and finality | **Already supported for current prototype; validate corrections** | Required stat fields and CONCLUDED/POSTGAME behaviour are parsed/tested. Need correction/`as of` or freshness semantics. [APP][TEST] |
| Participation/DNP evidence | **Upstream enhancement likely required** | Absence/zero stat line is insufficient; need named/emergency/substitute/played/late-withdrawal evidence where source permits. Scorer ruling stays BBBFFL. [PLAN] |
| AFL team lists and availability/status | **Upstream enhancement likely required or endpoint validation** | Season model assigns factual team lists/status/injuries upstream; current adapter exposes none. [PLAN][APP] |
| Ordinary team byes | **Probably supported; validate** | Round response documents `byes`, but representation and team identifiers are not parsed/tested. [APP][PLAN] |
| Match timeline/lockout evidence | **Probably supported for scheduled start; enhancement may be needed** | Start time exists in documented match response but is discarded. Validate reschedules and authoritative actual-start signal. [APP][INFERENCE] |
| Interchange/bench AFL facts | **Upstream only if useful factual evidence exists** | Could aid participation recommendations; BBBFFL Interchange choice and scoring remain BBBFFL concerns. [PLAN] |
| AFL response caching, fantasy fixture, lockout, DNP decision, scoring, ownership | **BBBFFL concern only** | Cache is non-authoritative; all fantasy derivation belongs here. [PLAN] |

### Potential issues for the `afl-api` repository

Do not create these until its current public contract/backlog is checked:

1. **Expose a bulk season player-list endpoint with canonical player ID and season/as-of AFL team membership.**
2. **Document and test historical season/round/match/stat retention and discovery for deterministic consumer replay.**
3. **Expose structured player participation/team-list/late-withdrawal/substitute evidence, including explicit unknown states.**
4. **Document round bye objects with stable AFL team IDs and unusual-round semantics.**
5. **Document scheduled, rescheduled and actual match-start timestamps for consumer lockout decisions.**
6. **Document POSTGAME→CONCLUDED and subsequent stat-correction/version/freshness semantics.**
7. **Provide contract fixtures or an OpenAPI compatibility gate for BBBFFL's consumed v1 subset.**
8. **Confirm whether historical point-in-time player team membership is supported; add it if current-team-only identity would rewrite history.**

No missing capability should be bypassed with direct provider access in BBBFFL.

## 11. Decision and risk register

### Confirmed rules — implement without reopening

- Ten teams, annual redraft, unique seasonal ownership, variable authorised squad target; preseason snake and mid-season reverse-ladder non-snake formats. [PLAN]
- Nine positions, no AFL-position eligibility, unchanged integer scoring formulas. [DECISIONS][PLAN]
- Submitted teams become visible immediately; private saved drafts do not; unlocked submissions can be revised. [DECISIONS]
- Website is primary, scorer proxy entry remains possible, and coach identity differs from season team identity. [DECISIONS]
- Ordinary AFL club byes are unavailable, not DNP, warn but do not prohibit selection. [DECISIONS]
- DNP and Interchange outcomes remain scorer-confirmed; highest-scoring eligible replacement wins even over an earlier intentional vacancy. [DECISIONS]
- Draft selections may be traded with scorer/admin approval and audit; replacement after draw is discretionary/admin-adjustable. [DECISIONS]
- Ladder is points, percentage, PF; exact equality is escalated. Finals ties favour higher locked home-and-away seed subject to scorer confirmation. [PLAN][DECISIONS]
- Top-five four-week finals; four separate SuperScore rounds for all ten. Normal/finals records share scope; SuperScore records remain separate; bye records may be annotated. [PLAN][DECISIONS]
- Broad spectator visibility is accepted; contacts, drafts and writes remain private. Branding is deferred. [DECISIONS]

### Implementation choices — engineering can propose and document

| Choice | Recommended default / risk control |
|---|---|
| Database | PostgreSQL in production for constraints/concurrent writes; SQLite remains excellent for tests/local replay. Confirm hosting cost before final choice. |
| Authentication mechanism | Simple managed password or email one-time-link flow with secure server sessions; avoid OAuth/enterprise IAM unless it reduces operator burden. |
| Audit representation | Append-only domain audit events plus versioned current entities; redact private fields from public/read logs. |
| Realtime | HTTP polling initially (already proven); consider SSE only if measured load/UX warrants it. |
| Background jobs | Small explicit scheduler/worker boundary with idempotent jobs; not a separate service unless operations require it. |
| Cache | TTL/provenance response cache for resilience; never editable AFL truth and never sufficient to sign off stale stats silently. |
| Correction model | New official result version/reopen event with reason; retain prior snapshot and recompute dependent ladder/finals deliberately. |
| Notification provider | Port/adapter with test sink; start with the easiest reliable email/WhatsApp-compatible mechanism after core state events exist. |

### Replay-validation questions

- Can every 2026 AFL round, including unusual structures, be discovered and replayed through v1 with stable final stats?
- Do the implemented fixture rotation and round mappings reproduce all 20 regular rounds exactly?
- Which lineups, DNPs, intentional vacancies, lockout times and mid-season transactions are known versus reconstructable?
- Does automatic best-Interchange recommendation reproduce scorer-confirmed historical outcomes, including ties between positions?
- How should stale AFL data and late corrections appear before and after BBBFFL sign-off?
- Is the coach UI's saved-draft/submitted/partially locked model understandable under mobile rehearsal?
- Are notification timings helpful rather than noisy, and are replay destinations isolated?

### Genuine league/scorer decisions — do not silently hard-code

| Decision | Needed by | Safe interim handling |
|---|---|---|
| Partial early submission followed by no main submission: which empty slots carry forward and how conflicts resolve | Before package 22/23 acceptance and live Round 1 | Implement scenario variants/recommendation; require scorer confirmation and audit. |
| Saved draft never submitted: confirm that it is ignored and normal previous-lineup fallback applies | Before package 22 acceptance | Current safe reading is “Save Draft is not Submit”; flag in replay/usability sign-off. |
| Exact ladder equality after points, percentage and PF | Before it can affect mid-season priority or finals seeding | Surface equality and require recorded scorer ruling; no invented fourth tiebreak. |
| Fee/payment/prize configuration retained in launch scope and exact prize splits | Before season setup/prize settlement UI | Track minimally/manual if not confirmed; do not block play. |
| Scorer correction/reopen authority and downstream finals response once a later correction changes standings | Before package 28/35 acceptance | Require explicit scorer procedure and reason; never silently rewrite bracket. |
| Tie among equal highest Interchange position scores (position choice may affect presentation/history though not total) | Before package 27 acceptance | Surface tied recommendations for scorer selection. |
| Opening Round/deferred-stat exceptional mappings for 2027 AFL fixture | Once AFL fixture is known, before season mapping freezes | Season-specific scorer configuration; no ordinary-bye inference. |

### Principal engineering/operational risks

| Risk | Mitigation / gate |
|---|---|
| Historical API coverage is inadequate | Validate 04 before replay schema hardens; file upstream issues, retain controlled evidence fixtures, never scrape providers. |
| Prototype schema leaks into full model | 01/03 design gate and explicit migration; preserve core logic, not magic identifiers. |
| Lockout races or clock/timezone errors | Authoritative timestamps, UTC storage, fake-clock tests, transactional version checks and scorer correction audit. |
| AFL correction after sign-off | Freshness/finality metadata plus explicit reopen/new result version and dependent-recalculation runbook. |
| Authentication delays product work | Build season identity first, use a simple mechanism, develop service APIs with role context; do not expose permissive admin publicly. |
| Replay evidence is incomplete | Four-class manifest and scorer-owned unknown register; synthetic tests remain labelled. |
| Roadmap duplicates unseen GitHub work | Mandatory tracker deduplication as immediate action; this environment could not inspect live Issues/Projects. |
| Scope crowds out Round 1 | Protect critical path, keep P3 out of launch, allow documented manual fee/notification/history processes. |

## 12. Deferred and explicitly non-blocking capabilities

These should not block a functional 2027 season unless later evidence changes priority:

- complete migration to 2004 and exhaustive reconstruction of old scoring rules;
- elaborate all-time analytics, draft trends and record visualisations beyond preserving 2027 facts and required separate record context;
- sophisticated logos, colours, animations and custom team branding;
- AI commentary, projections, optimal-team analytics and injury prediction;
- native mobile applications when a responsive website is sufficient;
- websockets/event streaming, distributed caches and excessive realtime infrastructure before polling is measured inadequate;
- new microservices or a duplicated AFL database;
- complex OAuth/enterprise identity for approximately ten coaches;
- multi-channel notification orchestration, chatbot commands or production WhatsApp automation if email/manual reminders cover launch;
- automatic fee collection/accounting; lightweight paid status is enough if retained;
- wholesale legacy GAS/workbook reimplementation or bidirectional Sheets synchronisation;
- pixel-perfect historical workbook presentation conventions.

Deferral does not mean data loss: the 2027 schema must preserve season, coach/team name, ownership, drafts, lineups, results, finals, SuperScore and audit provenance so richer history can be derived later. [PLAN]

## 13. Recommended immediate next actions

1. Review this roadmap with scorer/product owner, especially the genuine decisions and replay evidence classes.
2. Authenticate to GitHub and reconcile all 40 candidates with existing Issues, labels, milestones, open/merged PRs and Project fields; create only reviewed, non-duplicate issues.
3. Confirm the seven milestone names/exit gates and make **R — 2026 replay** a release criterion, not a demo label.
4. Start packages 01, 02 and 04: persistence/migration decision, audit boundary, and `afl-api` contract/history validation are the highest-risk foundations.
5. Begin package 08 evidence manifest in parallel; catalogue sources and gaps without waiting for the full replay harness.
6. Write architecture decision records for database, auth/session and final-result correction choices; none requires coach voting.
7. Preserve and run the existing prototype suite unchanged while introducing season context; reject scoring rewrites without a demonstrated defect.
8. Plan a thin vertical R1 slice before polishing broad screens: one persisted season, five pairings, owned squads, submitted lineups, controlled AFL facts, finalisation and ladder.
9. Schedule scorer review after R2 and again after R5; convert discrepancies into issues or priority changes before live 2027.
10. Work backwards from the 2027 draft and AFL Round 1 with contingency time for upstream AFL contract changes and a documented manual fallback.

## 14. Traceability notes

- Domain rules and product direction: `docs/plans/2027-season-model.md` and confirmed superseding decisions in `docs/plans/2027-season-decisions.md` / `2026-grand-final-coach-discussion.md`.
- Historical fixture, ladder, finals, SuperScore, records and replay evidence: `docs/plans/2026-workbook-findings.md`.
- Intended vertical slice: `docs/plans/2027-grand-final-prototype-brief.md`; actual classification is based on `bbbffl_app/app/`, `bbbffl_app/tests/`, runtime files and CI rather than assuming the brief remained exact.
- Legacy behavioural/operational evidence: `legacy/gas/` and `legacy/gas/docs/reviews/2025-system-forensic-review.md`; these are not target architecture.
- GitHub tracker caveat: local merge history and `.github/workflows/ci.yml` were inspectable; live Issues/labels/milestones/Projects were not accessible in this environment and must be checked before candidate creation.
