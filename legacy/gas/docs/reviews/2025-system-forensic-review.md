# The 2025 BBBFFL system, as it actually ran
“Preserved forensic analysis of the historical BBBFFL implementation, produced during investigation of the 2025 codebase/audit branch. This document records findings for reference during planning of the 2027 system and is not itself a normative architecture specification.”

A code-level reconstruction of three Google Apps Script projects — what shipped to GitHub, what was changed live in the editor afterward, and what a 2027 rebuild should keep, rebuild, or throw away.

- **Baseline:** `main` (published June 2025)
- **Live state:** `audit/google-apps-script-2026` (reconstructed from the Apps Script editor)
- **Review type:** read-only; no historical code or other documentation modified
- **Date:** 16 Aug 2026

## How to read this document

Claims are labeled by evidentiary weight throughout:

- **FACT** — directly observed in the code, the branch diff, or the runtime state supplied in the review brief.
- **INFERENCE** — a reasonable reading of the evidence, but not provable from the repository alone.
- **BUG** — a defect found by reading the live code path, with a concrete failure condition.

---

## Part One — Forensic description

### 1. Component responsibilities & data flow

**FACT** The system is three independent `clasp`-managed Apps Script projects, each bound to its own Google Sheet, pushed/pulled with `push-all.sh` / `pull-all.sh`. There is no shared library, no shared source of truth for scoring logic, and no CI — every deploy is a manual `clasp push`, which is exactly the gap that let the editor drift the audit branch captures.

```
External data                     AFL_Stats project
┌─────────────────────────┐       ┌───────────────────────────────┐
│ afl-api.thehardinghams   │──────▶│ Game Schedule · Player Names   │
│  .net (custom API)       │       │ Live Stats · Round N · Teams   │
│ v1.afl.api-sports.io     │┄┄┄┄┄┄▶│ Injury List · Dashboard        │
│  (legacy, being retired) │       └───────────────┬─────────────────┘
│ footywire.com (scrape)   │┄┄┄ (unattached) ┄┄┄┄┘  │
└─────────────────────────┘                        │
                                                      ▼
10 team Google Forms          BBBFFL_Weekly_Teams project
┌───────────────────┐         ┌─────────────────────────────────┐
│ Form Responses N   │───────▶│ Master Weekly Teams · Holding    │
│ (per team)          │◀──────│ Sheet · Coach Emails · Pre-Filled│
└───────────────────┘  links  │ Links · Dashboard                │
                                └───────────────┬───────────────────┘
                                                  │
                                                  ▼
                                BBBFFL_Results project
                                ┌───────────────────────────────────┐
                                │ Master Results · Ladder · Fixtures  │
                                │ Local Overrides · Bye Replace       │
                                │ Scorer Review · Live Round N · Rnd N│
                                └───────────────┬─────────────────────┘
                                                  │  coach email + override loop
                                                  ▼
                                        back to BBBFFL_Weekly_Teams
```

Each project owns a distinct slice, and every cross-project read is a live `SpreadsheetApp.openById()` call rather than a shared API or database — the spreadsheets themselves are the integration layer:

| Project | Owns | Reads from |
|---|---|---|
| `AFL_Stats` | Raw AFL schedule, player identity, live and round-final stats | External APIs only |
| `BBBFFL_Weekly_Teams` | Coach form intake, submission validation, consolidated team-of-record | `AFL_Stats` (schedule, player→club lookups) |
| `BBBFFL_Results` | Fantasy scoring, live scoreboard, ladder, fixtures, scorer overrides | `AFL_Stats` (stats) + `BBBFFL_Weekly_Teams` (team selections) |

### 2. External API endpoints & authentication

| Endpoint | Auth | Used by | Status |
|---|---|---|---|
| `afl-api.thehardinghams.net/api/{rounds,matches,players,player-stats}` | Header `x-api-key`, script property `AFL_API_KEY` | Schedule, player names, both round/live stats fetchers | **FACT** live, primary path |
| `v1.afl.api-sports.io` (api-football.com) | Header `x-apisports-key`, script property `API_KEY` | `fetchAFLTeams`, `fetchAFLPlayerNamesOld`, `fetchAFLGameScheduleFromOldApi`, `fetchAFLStatsOld` | **FACT** parked — README states it is being fully retired |
| `footywire.com/afl/footy/injury_list` | None (spoofed `User-Agent`) | `updateInjurySheet` | **FACT** writes an "Injury List" sheet with no downstream consumer anywhere in the codebase |

**INFERENCE** `afl-api.thehardinghams.net` is almost certainly the predecessor of the "upstream `afl-api` application" referenced in the 2027 brief — this review treats it as the same lineage, now being deliberately hardened into a stable consumer-facing API.

### 3. Sheets & Forms as interfaces / state

Google Sheets plays three roles simultaneously in this system, never separated: **UI** (formulas, `HYPERLINK` pre-fill links, checkboxes, dropdown validation), **datastore** (Master Results, Master Weekly Teams, Local Overrides, Bye Replace are the only copies of this data anywhere), and **job queue / dashboard** — each project's `Dashboard` sheet logs every script's last-run time and note, and several `monitor*` functions read another project's Dashboard sheet to decide whether to re-run downstream work. This is a hand-built polling/dedup mechanism standing in for a real event system.

**FACT** Ten near-identical Google Forms (one per team) feed ten `Form Responses N` sheets inside the Weekly_Teams spreadsheet. All ten forms share the same `entry.NNNNNN` field IDs for the round and the nine player slots — only the team-name field ID differs per team — which is strong evidence the forms were cloned from one template rather than built independently.

### 4. Scheduling & triggers

| Project | Mechanism | Confirmed active? | What it actually does |
|---|---|---|---|
| `AFL_Stats` | Time trigger → `fetchAFLGameSchedule` | **FACT** yes (per brief) | Rewrites the whole Game Schedule sheet from `/api/rounds` + `/api/matches` |
| `AFL_Stats` | Time trigger → `monitorMatchStatus` | **FACT** yes (per brief) | Lives, oddly, in a file named `orchestrateAFLUpdates.js` — no function of that name exists anywhere in the repo, a rename that was started and never finished. Re-runs the schedule fetch if >6h stale or a match is imminent/live, then conditionally runs player-name, live-stats and round-final fetches. |
| `AFL_Stats` | `scheduleMonitorIfLive` (self-rescheduling 5-min trigger) | **FACT** disabled (per brief) | Code exists and would, once armed, re-poll every 5 min only while a match is live, then delete its own trigger — a second, independent "is anything live" heuristic layered on top of `monitorMatchStatus` |
| `AFL_Stats` | `scheduleAFLStatsUpdate` (self-rescheduling 6-hr trigger) | **INFERENCE** unknown — not named in the brief | A *third* independent round-completion poller, functionally overlapping the other two |
| `BBBFFL_Results` | Time trigger → `monitorWeeklyTeamUpdates` | **FACT** yes (per brief) | Compares last-run timestamps across two spreadsheets' Dashboard sheets; re-runs `fetchBBBFFLResults` if team selections changed since results were last computed |
| `BBBFFL_Results` | `monitorAFLStatus` (separate function, same project) | **INFERENCE** unknown — not named in the brief | Gates `generateLiveBBBFFLMatches` behind the `SEASON_MONITORING_ENABLED` flag; if this isn't the scheduled trigger, live-match generation may only ever run via the manual menu button — see the bug below |
| `BBBFFL_Weekly_Teams` | Installable spreadsheet `onFormSubmit` trigger | **FACT** yes (per brief) | Necessarily installable, not simple — the handler sends email and opens other spreadsheets, which requires full authorization |

> **BUG — the live season-monitoring kill-switch is broken on its most-used path.**
> The editor-only change to `BBBFFL_Results/getConfig.js` adds `SEASON_MONITORING_ENABLED`, read correctly in `monitorAFLStatus.js` as `config.SEASON_MONITORING_ENABLED`. But the matching edit to `generateLiveBBBFFLMatches.js` checks a bare `SEASON_MONITORING_ENABLED` — a global that is never declared anywhere in the project. Calling `generateLiveBBBFFLMatches()` directly — which is exactly what the "📺 Update Live Round" menu item (`runLiveBBBFFLForCurrentRound`) and `test.js` do — throws `ReferenceError: SEASON_MONITORING_ENABLED is not defined` on every invocation, regardless of the flag's value. Given the known script property `SEASON_MONITORING_ENABLED=false`, the manual live-scoreboard button was very likely broken for the entire period this edit was live and never pushed back to `main`.

### 5. AFL player identity & matching

**FACT** The system carries the scars of a mid-season provider migration (as the README's "Transition Note" states outright). At least three player-ID schemes coexist in the repository:

- **`afl_id`** from the new custom API — the canonical key used end-to-end for stats joins and fantasy scoring.
- **Legacy api-sports.io numeric IDs** — used only by the parked old-API fetchers.
- **Draft-list IDs** in a "2025 Draft List" sheet, reconciled against the new scheme by `matchPlayerNamesToIDs.js`.

AFL_Stats maintains two independently-populated, overlapping player tables: `Player Names` (simple output of `fetchAFLPlayerNames`, refreshed on the live schedule) and `Mapped AFL Players` (output of `populatePlayersSheetfromNewapi` + `mapPlayersWithFallbacks`, a fuzzy full-name+club → surname+club reconciliation against legacy IDs, with a match-source tally).

**BUG risk** `BBBFFL_Weekly_Teams`'s `lookupAFLPlayer()` — the function that resolves a submitted player to their AFL club for lockout-timing validation — reads `Mapped AFL Players`, not `Player Names`. Nothing in the confirmed trigger set refreshes `Mapped AFL Players` on a schedule; it appears to depend on someone manually re-running the reconciliation pair. If it goes stale (e.g. after a trade), submission-lockout validation can silently use an out-of-date club assignment even while scoring itself is working from fresh data.

**FACT** AFL_Stats also does real presentation-layer cleanup the upstream API apparently doesn't guarantee: `toProperCaseName()` handles Mc/Mac/O'/hyphenated/"van"-prefixed surnames, and team short-codes are hand-maintained in two separate dictionaries (`getTeamShortName.js` and an inline object inside `fetchAFLTeams.js`).

### 6. Weekly team submission & validation

**FACT** `onFormSubmit(e)` is the single entry point. It runs, in order:

1. **Duplicate check** — no player ID may appear twice in the nine slots.
2. **Locked-player check** — for any slot whose player's AFL match has already started (`playerAlreadyStarted`, computed against the live Game Schedule), the submitted ID must match the previous stored submission or the whole submission is rejected and routed to the coach with the specific offending slot named.
3. **Late-submission check** — if any submitted player belongs to a club in the round's *first* match and the timestamp is after kickoff, and there is no prior submission to fall back on, the row is held for scorer review rather than rejected outright.
4. **Hard lockout** — a submission timestamped after the round's *second* match kicks off is unconditionally held, regardless of which players changed.

Valid submissions flow into `consolidateWeeklyTeams()`, which dedupes across all ten `Form Responses N` sheets down to one newest row per team+round in `Master Weekly Teams` — **this sheet, not any individual form response, is the team-of-record** that scoring reads from.

Held submissions land in a second human-review queue, the `Holding Sheet`, with a checkbox/status-dropdown workflow (`reviewHeldSubmissions`): approving re-runs consolidation and re-notifies the coach; declining copies the team's *previous round's* line-up forward as a fallback (`insertPreviousRoundAsOverride`) and emails an explanation.

**FACT** The audit-branch diff to `validateTeamSubmission.js` is a genuine, already-applied bug fix that never reached GitHub: the old logic flagged a locked player as "changed" using an unconditional `currentId !== previousId`, which misfired whenever there was no prior submission to compare against. The live fix requires `previousId && currentId !== previousId`. It also deletes a fully superseded helper, `saveToHoldingSheet()`, left over from before the current `Holding Sheet` workflow existed.

### 7. Live vs. final statistics

**FACT** "Live Stats" is a single sheet, wholesale cleared and rewritten on every `fetchLiveAFLPlayerStats` run. A round's "Round N" sheet accumulates de-duplicated *final* rows once a match's status reads `FT` — and "final" here is a heuristic, not a signal from upstream: the code estimates full-time as **kickoff + 2.5 hours** and uses that estimate both to decide which Live Stats rows are safe to prune and to timestamp the final record.

**BUG — correctness risk** Any match delayed, extended, or interrupted (weather, crowd incidents, extra-time conventions) beyond that fixed window would have its stats miscategorised by this timer, purely because the API integration doesn't (or didn't, at the time) expose a genuine completion flag.

**FACT** There are two separately-coded merge engines that are supposed to converge on the same numbers but never share code: `generateLiveBBBFFLMatches()` builds the in-progress scoreboard (Live > Round-final > manual override > bye, tagged by source for on-sheet colour coding only), while `fetchBBBFFLResults()` independently computes the official `Master Results` row (interchange override > manual override > FT stats > bye) with its own terminal "✔️ FT" vs "🟡 Live" status logic. Nothing enforces that these two pipelines agree during edge cases — byes, overrides, and DNPs are each re-implemented twice.

### 8. BBBFFL scoring & result generation

| Position | Formula |
|---|---|
| Forward (×3) | 6 × goals + behinds |
| Midfield (×3) | total disposals |
| Ruck | marks + hitouts |
| Tackler | 6 × tackles |
| Interchange | scored as whichever position it replaced — the code explicitly refuses to score "Interchange" as itself and returns 0 with a warning if that ever happens |

**FACT** This exact formula is implemented twice, independently — `calculateFantasyPoints()` in `fetchBBBFFLResults.js` and `getPositionScore()` in `generateLiveBBBFFLMatches.js` — with no shared function. `calculateFantasyPoints()` also contains a dead, unreachable `Logger.log` line inside its `switch` referencing an undefined variable (`replacement`), left over from an earlier refactor.

**FACT** The ladder (`generateBBBFFLLadder`) awards 4 points for a win, 2 for a draw, sorts by points → percentage → points-for, and — critically — only counts a round once **all ten** team rows for it show "✔️ FT" (`getMaxCompletedRound`). A round with nine finished matches and one still live is excluded from the ladder entirely, not partially credited.

**FACT** Fixtures are generated once (`generateBBBFFLFixtures`) from a random draw using a fixed 9-round round-robin skeleton, mirrored home/away for rounds 10–18, then cycled modulo the base fixtures for round 19 onward — so rounds 21–24 (the SuperScore rounds) reuse an earlier round's matchups programmatically, with no bespoke fixture logic of their own.

### 9. SuperScore behaviour

**FACT** Every trace of "SuperScore" in the repository is confined to the editor-only diff on `audit/google-apps-script-2026`, and entirely within `BBBFFL_Weekly_Teams`'s link-generation layer. For rounds 21–24, labelled `SS1`–`SS4`, `renderPreFilledLinksSheet()` adds one extra "SuperScore Link" column: a second pre-filled form link per team, built with all nine player slots blank and a distinct round label.

**FACT** No corresponding logic exists in `AFL_Stats` or `BBBFFL_Results` — no scoring rule, no ladder treatment, no validation path that recognises an "SS" round distinctly from a numeric one. `validateTeamSubmission` and `onFormSubmit` both `parseInt` the round field, so a literal "SS1" typed into that field wouldn't parse as a usable round at all.

**INFERENCE** SuperScore reads as a feature under active construction at the point this state was captured — the coach-facing entry point exists, but its actual selection and scoring rules are either undocumented in this repository, handled manually by the commissioner outside the system, or simply unfinished. The evidence supports "incomplete," not "broken as designed."

### 10. Error handling & operational controls

**FACT** Error handling is uniformly log-and-continue: `try/catch` around individual fetches with `muteHttpExceptions:true`, `Logger.log`/`logAction` the failure, move on. Nothing alerts a human when an API call fails — the only feedback loop is a coach or scorer noticing missing data downstream. `checkApiQuota()` — which only guards the legacy api-sports.io calls — inspects rate-limit headers and sleeps 2 seconds or bails; there is no retry/backoff, and the new `afl-api` integration has no quota handling of any kind.

**FACT** The one substantial operational control is the **Scorer Review** system (`generateScorerReview` / `processApprovedOverrides`): a rules engine that flags any player slot that's blank, not found in the API, scored zero, or otherwise ambiguous, and offers a scorer a checkbox + status-dropdown UI to approve manual overrides or mark a player DNP — which then feed `Local Overrides` and are honoured by both scoring engines. The Weekly_Teams `Holding Sheet` is a structurally similar but entirely separate second review queue, independently implemented with no shared code between the two projects.

**BUG** `AFL_Stats/onOpen.js` wires its "Dashboard" menu to `fetchBBBFFLStatsForRound` and `updateAllBBBFFLForms` — neither function exists anywhere in the codebase. Both menu items throw if clicked.

### 11. Legacy, dead & transitional code

| Item | Location | Status |
|---|---|---|
| api-sports.io integration (`fetchAFLTeams`, `*Old` functions) | `AFL_Stats` | **FACT** parked, README confirms retirement is planned |
| Duplicate `fetchAFLStats()` implementations | `fetchAFLStats.js` *and* `fetchAFLStatsFromNewApi.js`, same project | **BUG — ambiguous at runtime.** GAS file-concatenation order (not visible in git) decides which definition wins when `fetchManualRound()` calls `fetchAFLStats(24, true)` |
| `orchestrateAFLUpdates.js` | `AFL_Stats` | Contains `monitorMatchStatus()`; no `orchestrateAFLUpdates` function exists — an unfinished rename |
| `matchPlayerNamesToIDs.js` | `BBBFFL_Weekly_Teams` | Hardcoded to one team's form sheet ("Form Responses 12") and the draft list — a one-off seeding utility, not wired to any live path |
| `saveToHoldingSheet()` | Removed live from `validateTeamSubmission.js` | Fully superseded by `holdLateSubmission()`; correctly deleted in the editor, still present on `main` |
| Duplicate `suggestCurrentRoundScorerReview()` | `BBBFFL_Weekly_Teams/generatePreFilledLinks.js` | **BUG — broken.** Apparent copy-paste from `BBBFFL_Results/ScorerReview.js`, references an undefined `reviewSheet` and calls `Sheets.Spreadsheets.batchUpdate` although the Sheets Advanced Service is never enabled in this project's manifest. Wired to the "Scorer Tools" menu — clicking it throws. |
| `test.js` / `myFunction()` in every project | All three projects | Ad hoc scratch entry points, hand-edited live to poke specific rounds/games — a developer console left inside production, and the exact file the branch diff shows being edited directly |

### 12. Where Apps Script and the AFL API duplicate each other

- **Scoring formula** implemented twice (see §8) — must be hand-kept in sync.
- **Player identity** resolved twice from the same `/api/players` endpoint into two overlapping sheets (see §5).
- **Match status** inferred client-side by a free-text label mapper (`getStatusShortCode()`) rather than the API exposing a stable enum.
- **Match finality** estimated by a fixed kickoff+2.5h timer in Apps Script rather than a completion flag from upstream (see §7).
- **Name/team normalisation** (proper-casing, short-codes) done defensively in Apps Script against raw API text.
- **Quota management** is bespoke, inconsistent, and only covers the legacy provider.
- **"Is anything live" polling** is reimplemented three separate times across `scheduleMonitorIfLive`, `scheduleAFLStatsUpdate`, and `monitorMatchStatus`, each against the same Game Schedule sheet.

---

## Part Two — 2027 architecture assessment

### 13. Framing

If `afl-api` is now being deliberately built to isolate consumers from Champion Data/AFL collector churn, its job is to make everything in §12 *disappear* from BBBFFL's codebase: a stable player identity, a stable match-status enum, a genuine completion flag, and normalised names — so BBBFFL never again needs to guess finality from a stopwatch or reconcile two player tables by fuzzy string matching. What's left over is BBBFFL's own business logic (scoring, team validation, ladder/fixtures) and its coach-facing workflow — neither of which belongs to the API at all.

### 14. Responsibility → recommended home

| Responsibility | 2025 home | 2027 recommended home | Why |
|---|---|---|---|
| Raw AFL ingestion, name/team normalisation, match-status & completion signals | Apps Script (`AFL_Stats`) | **afl-api** | Exactly the churn-isolation the 2027 brief describes; removes the 2.5h finality timer and dual player tables |
| Canonical player identity | Apps Script, two overlapping sheets | **afl-api** | One endpoint, one ID, no fuzzy reconciliation as an ongoing cost |
| Fantasy scoring formula, interchange/bye/override precedence | Apps Script, duplicated twice | **Dedicated BBBFFL service** | This is BBBFFL's actual IP; it must exist exactly once and be testable |
| Weekly submission validation (duplicates, lockouts, late rules) | Apps Script, tightly coupled to Sheets | **Dedicated BBBFFL service**, fronted by a thin GAS layer | Genuinely good rules, hand-built and unstable; worth preserving, not worth leaving untested |
| Coach-facing submission UI | Google Forms | **Sheets/Forms** (as UI only) | Ten trusted users, zero training tolerance — Forms is still the right tool for this one job |
| Ladder / fixtures / results display | Apps Script computes *and* displays | **Sheets** displays; **BBBFFL service** computes | The shared-scoreboard experience is genuinely good; the computation shouldn't live where it's untestable |
| Scorer Review / Holding Sheet human review | Apps Script, two parallel implementations | **Sheets** as review surface, backed by **BBBFFL service** data | Checkbox/dropdown review is a fine lightweight admin panel at this scale — keep the pattern, not the duplication |
| Live-match scheduling/orchestration | Three competing Apps Script self-rescheduling triggers | **afl-api** (push/cheap "what's live" endpoint) + **BBBFFL service** cron | Minute-granularity GAS triggers with no shared state are the wrong tool for time-sensitive polling |
| SuperScore | Half-built link generator only | **Dedicated BBBFFL service**, once rules are defined | Needs to be finished as part of scoring, not left orphaned in the forms layer |

### 15. Is Google Apps Script still appropriate in 2027?

Not an all-or-nothing question. **Apps Script and Sheets/Forms remain a good fit** for exactly the surface they're already good at here: a small, trusted, ~10-person coach-facing intake form, and a commissioner's manual-review workflow where a checkbox and a status dropdown are honestly a perfectly adequate admin panel. Zero hosting cost, zero login friction, and the league already knows how to use it.

**Apps Script is the wrong place** to be the system of record or the only implementation of fantasy scoring rules, and the wrong place to run time-sensitive live-match orchestration. The evidence for this is in the audit itself: cross-project reads are slow synchronous `openById()` round-trips, there is no test suite or staging environment, deploys are a manual `clasp push` with no review gate, and — as a direct consequence — a live, unpushed `ReferenceError` in the season kill-switch shipped silently for an unknown period with nobody able to catch it before a coach did. The right shape for 2027 is Apps Script/Sheets as a thin, replaceable *client* of a real service — never the service itself.

### 16. Keep / Refactor / Move out of Apps Script / Retire

**Keep**
- The 10 team Google Forms as coach input surface
- Sheets as the ladder/fixtures/results *display* surface
- The Scorer Review + Holding Sheet checkbox/dropdown review pattern

**Refactor**
- Scoring formula + interchange/bye/override precedence — one implementation, called by both live and final views
- Weekly submission validation rules — same rules, moved off Sheets-as-database
- Live vs. final read model — one merge engine, not two
- Season kill-switch — a real, consistently-applied flag, not a typo'd global

**Move out of Apps Script**
- All Champion Data/AFL ingestion, parsing, normalisation → `afl-api`
- Player identity reconciliation → `afl-api`
- Live-match scheduling/orchestration → real cron/webhooks, not three competing GAS triggers
- Fixture/ladder computation → BBBFFL service; Sheets only renders the output

**Retire**
- Legacy api-sports.io integration (README already calls this)
- Footywire injury-list scrape (unauthenticated, never wired downstream)
- One of the two `fetchAFLStats()` implementations
- `orchestrateAFLUpdates.js`, `matchPlayerNamesToIDs.js`, and other dead one-offs
- Broken `onOpen()` menu items and the duplicated, broken `suggestCurrentRoundScorerReview`
- `test.js`/`myFunction()` as a production debugging surface — replace with real staging

### 17. Smallest sensible proof of concept

The goal isn't to rebuild BBBFFL — it's to prove the split in §14 actually removes the duplication found in this audit, on the smallest possible slice.

1. **afl-api exposes three stable endpoints** — players (canonical ID, normalised name), matches (a real status enum, not free text), and player-stats (with an explicit *final* flag, not a timer) — behind a single API key.
2. **One BBBFFL service owns the scoring formula**, implemented exactly once, plus the validation rules and fixture list for a single test round and a single team.
3. **One Google Form + a thin Apps Script webhook forwarder** for that one team — Apps Script does nothing but relay the submission to the service and email a confirmation.
4. **One read surface** (a Sheet or a simple page) rendering that round's live and final result, sourced from the service's single read model.
5. **Success criterion:** for one round and one team, the live number and the final number are produced by the same code path and are provably identical — the exact guarantee the 2025 system, with its two independent merge engines, could never make.

---

## Methodology

This review compared `main` and `audit/google-apps-script-2026` via `git diff`, then read every source file in the reconstructed live-state branch across all three Apps Script projects (45 files, ~5,700 lines). No historical code or other documentation in the repository was modified as part of this review. Claims marked **FACT** are drawn directly from source, the branch diff, or the runtime state supplied in the brief; claims marked **INFERENCE** go beyond what the repository alone can prove.
