# 2026 full-season replay: evidence summary

This document synthesises the three separately recorded 2026 historical
replay phases into one full-season view. It does not duplicate their
detail and does not replace them as the primary evidence record:

1. [`docs/evidence/2026-first-half-replay/`](2026-first-half-replay/) --
   Opening Round + Rounds 1-9.
2. [`docs/evidence/2026-second-half-replay/`](2026-second-half-replay/) --
   Round 10, the reconstructed mid-season draft, Rounds 11-20.
3. [`docs/evidence/2026-finals-replay/`](2026-finals-replay/) -- Finals
   Weeks 1-4, SuperScore SS1-SS4, and season completion/archive.

For the current-state readiness conclusion this evidence supports, see
[`docs/2027-live-season-readiness.md`](../2027-live-season-readiness.md).
That document is the authoritative v0.1/v0.2 classification; this one is
the evidence it draws on.

## What the full-season replay proved end-to-end

Across the three phases, the replay exercised a single, continuous 2026
league history against real persisted state, using the production domain
services (not a parallel simulation):

- season/competition bootstrap, the 220-selection preseason draft and
  opening-squad freeze;
- Opening Round deferred nominations and compensating-bye scoring;
- BBBFFL-to-AFL round mapping and staged selective/main lockout, repeated
  for 20 ordinary rounds;
- Coach, carry-forward and delegated (Scorer/Admin proxy) weekly-lineup
  submission;
- audited locked-lineup correction and missed-submission adjudication;
- scorer calculation, DNP/interchange review, publication and ladder
  accumulation;
- the historical Round 10 -> mid-season draft transition: reverse-ladder
  order, 28 delistings, 28 vacancy-derived selections, and a recovered
  post-draft trade;
- Rounds 11-20 on the reconstructed post-draft squads;
- Finals Weeks 1-4 and SuperScore SS1-SS4, run concurrently against the
  same shared AFL rounds, including bracket progression, Coach/SuperScore
  eligibility isolation, and Preliminary/Grand Final publication;
- season completion (`active -> completed`), Premiership/Wooden Spoon
  award creation, independent archival verification, and a real
  post-completion write-fence smoke test.

No preserved source replay database or checkpoint was mutated to produce
this evidence; every phase worked from a restored or continued *copy*.

## Browser-operated versus CLI-only, by phase

The three phases were not operated identically, and the summary below is
the load-bearing fact for the readiness document's v0.1/v0.2 split:

| Phase | How it was actually operated | Source |
|---|---|---|
| First half (Rounds 1-9) | Coach/Scorer/Admin browser dashboards existed from mid-replay onward (PRs #154, #156-160); season/round setup and replay-checkpoint control remained operator/CLI. | `2026-first-half-replay/workflow-findings.md`, `ux-findings.md` |
| Round 10 -> mid-season draft (original 2026 replay) | The mid-season draft **domain and CLI were sufficient for the historical replay**, but the next action was not obvious from any dashboard; the operator needed to know the CLI command and routinely translated UUIDs to team/player names by hand. | `2026-second-half-replay/ux-findings.md`, "Mid-season draft discoverability" |
| Rounds 11-20 | Delegated lineup entry and Scorer review/publication were routine browser operation; this was treated as a credible proxy for future individual-coach browser operation. | `2026-second-half-replay/workflow-findings.md` |
| Finals/SuperScore | Started CLI-only (issue #194 found `app.superscore_round`, `app.superscore_review` and `app.season_completion` had **no operator-reachable entry point at all** -- not even a route, only test coverage -- and added three new 2026-scoped CLI scripts to close that gap). Execution then shifted increasingly to the browser Scorer workflow for finals progression, review and publication as it matured; the CLI remained the recovery/operator primitive. | `2026-finals-replay/workflow-findings.md`, findings 1 and 11 |
| Season completion/archival | CLI-only throughout (`scripts/season_completion_2026.py`, `scripts/season_archival_checkpoint_2026.py`), including the `setup -> active` lifecycle repair the closeout needed before completion would proceed. | `2026-finals-replay/workflow-findings.md`, finding 10; `2026-finals-replay/provenance-manifest.md` |

**Since the original replay**, the mid-season draft gap above was closed on
current code: issue #181 (PR #225), acceptance findings under #226/#227/#228,
and the pre-season proxy-pick alignment in #229-#232 together produced a
complete browser workflow -- trigger-round configuration, human-readable
ordinary-competition selection, ladder preview/freeze, Coach and Scorer
delisting, the numbered draft table, Coach draft-board access (read-only
off-turn, private shortlist, own-turn selection), Scorer/Admin proxy
selection, and human-readable trade entry/approval -- with no UUID lookup
required. This was verified twice: once as the original 2026 domain replay
(CLI-driven, described above), and again, independently, as a **current-code
browser acceptance pass** against a disposable restored pre-draft checkpoint
(issue #224, 2026-09-20 comment). The second pass is the evidence that the
*current* mid-season draft is a normal-user workflow, not the first.

By contrast, **SuperScore round lifecycle setup** (stream/round creation,
AFL-mapping confirmation, opening) now has a browser path through the
combined Finals+SuperScore preflight action
(`app.finals_superscore_open.open_finals_and_superscore_week`,
issue #221), so the 2026-replay-era CLI gap for that specific step has
since closed. **Season completion** (`preview_complete_season` /
`complete_season`) and the `setup -> active` season-activation transition
still have no browser route as of this document -- see the readiness
document's v0.1-outstanding list.

## Historically reconstructed / synthetic / exceptional versus ordinary supported workflows

The replay is a *reconstruction* of a real league season, not a record
captured live in 2026, and it deliberately kept three kinds of evidence
distinct:

- **Ordinary supported workflows** -- the vast majority of rounds (all of
  Rounds 1, 3-5, 7-8, 11, 13-20; the routine share of every phase) used
  only the normal submit/lockout/score/review/publish path with no special
  handling.
- **Historically reconstructed exceptional actions**, applied through the
  *existing* audited Scorer correction/adjudication workflows, never a
  direct database edit:
  - Round 2/4 Opening Round deferred-value resolution;
  - Round 6 externally-agreed late change, recorded via audited correction;
  - Round 9 carry-forward disagreeing with an informal historical
    reconstruction (the rule-correct result was kept);
  - Round 12 bye-player selections, restored via audited lineup correction
    after issue #185/#186 made the position correctable pre-lockout;
  - the post-draft Crabs/Bridesmaids trade, replayed forward from a
    preserved pre-close boundary rather than patched directly;
  - the historical finals-seeding snapshot (issue #187/#188), which
    reproduces the actual 2026 finals order for exactly three teams
    affected by two known historical Scorer-error results, **without**
    rewriting the mathematical Round 20 ladder those errors are part of.
- **Synthetic/replay-only mechanics**, never part of live 2027 operation:
  replay-clock/checkpoint advancement to reveal AFL evidence; the disposable
  restore/migrate-forward pattern used for every regression and acceptance
  pass; the 2026-scoped finals-seeding snapshot tool itself (structurally
  incapable of running for any other season).

No replay finding asserts that a synthetic or reconstructed action is
itself a proven 2027 capability. Where a defect was found and fixed (see
below), the fix is a real code change proven by tests and/or later
regression, independent of the historical scenario that exposed it.

## Recovery checkpoints preserved

Each phase boundary has a verified, paired database-archive + replay-
checkpoint recovery point, checked with `pg_restore --list` and private
SHA-256 records (filenames and hashes are never committed):

| Boundary | Phase | Detail |
|---|---|---|
| Pre-pick-1 / draft-after-pick-200 | Preseason | First-half evidence manifest; also the source for the Stage A/B current-code regression below |
| Round 9 / first-half close | First half -> second half | `2026-first-half-replay/phase-one-closeout.md` |
| Pre-Round-10 | Second half | `2026-second-half-replay/provenance-manifest.md` |
| Round 16 | Second half (interim) | `2026-second-half-replay/provenance-manifest.md` |
| Round 20 / home-and-away | Second half -> finals | `2026-second-half-replay/provenance-manifest.md` |
| Finals-seeding snapshot applied | Finals handoff | `2026-second-half-replay/provenance-manifest.md`, `2026-finals-replay/provenance-manifest.md` |
| Pre-closeout (post Grand Final/SS4) | Finals -> completion | `2026-finals-replay/provenance-manifest.md` |
| Final archival checkpoint (`completed_season_version` 3) | Terminal | `2026-finals-replay/provenance-manifest.md` |

The finals/SuperScore phase's provenance manifest records an explicit,
disclosed evidence gap: several per-round backup filenames, hashes and
audit-event IDs from the post-seeding and individual Finals/SuperScore
rounds were not retained in the closeout record. This does not affect the
verified terminal state (all eight Finals/SuperScore rounds final before
completion, archival guard passed, final archive validated) but is recorded
here rather than reconstructed.

## Defects and UX gaps found and resolved during replay

This is a pointer list; each item's full analysis lives in the linked
phase document.

| Finding | Phase | Resolution |
|---|---|---|
| Dashboard assumed the latest final round is always in the active AFL package | Second half | Issue #176/PR #177 |
| Truncated 9-round fixture had no continuation path | Second half | Issue #178/PR #179 |
| PostgreSQL `FOR UPDATE` + aggregate rejected on trade approval | Second half | Issue #182 |
| Bye-player selections locked the whole selector, not just the invalid choice | Second half | Issue #185/PR #186 |
| Public ladder subtitle implied a future round had contributed | Second half | Issue #180 |
| `app.superscore_round`/`app.superscore_review`/`app.season_completion` had no operator-reachable surface | Finals | Issue #194, three new CLI scripts |
| PostgreSQL `FOR UPDATE` + aggregate rejected on SuperScore review-state check | Finals | Issue #194 (same class as the second-half finding, different module) |
| No completed-season write fence on `app.superscore_review` | Finals | Issue #194 (Codex P1 on PR #207) |
| SuperScore AFL-round mapping trusted an operator-suppliable identifier (multi-round review fix) | Finals | Issue #194, `resolve_concurrent_finals_afl_mapping` |
| `setup-round`/`open-round`/`ensure_round` accepted a non-SuperScore round/competition | Finals | Issue #194, `_require_superscore_round`/`_require_superscore_competition` guards |
| SuperScore mapping derivation read the mutable mapping head, not the finals week's frozen snapshot | Finals | Issue #194 |
| Playbook published a finals week before that round's SuperScore lineups were submitted | Finals | Issue #194 (playbook fix, not a code defect) |
| No stream-aware `open -> review` transition for finals/SuperScore | Finals | Issue #194, `advance_week_to_review`/`advance_round_to_review` |
| Season remained in `setup` through the entire replay until closeout blocked completion | Finals | Resolved via the supported `setup -> active` transition; **still no browser route for this gate**, see readiness document |
| Mid-season draft next action not discoverable; CLI/UUID-driven | Second half (original) | Issue #181/PR #225, acceptance findings #226-#232 (browser-acceptance-tested on current code, see above) |

Findings recorded as non-blocking presentation/UX items (Scorer dashboard
card layout, Coach weekly-selection chronology, same-week Finals/SuperScore
copy-to-draft, carry-forward-versus-private-draft clarity, Finals preflight
discoverability, Australian date presentation, ladder PPG-as-tiebreak
guard) are catalogued in each phase's own `ux-findings.md` and carried
into the readiness document's v0.1/v0.2 split rather than repeated here.

## Targeted current-code regression (Stages A-D)

The three phases above were completed at different historical application
baselines. #224 required a **targeted regression**, not a second full
replay, proving representative early-season scenarios still behave
correctly on current `main`. That regression is now complete; see
[`docs/2027-live-season-readiness.md`](../2027-live-season-readiness.md#current-code-regression-evidence-stages-a-d)
for the stage-by-stage record (source checkpoint, application/migration
head, scenario, expected-vs-actual, PASS/discrepancy). In summary:

- **Stage A** (pre-pick-1 preseason draft) -- PASS.
- **Stage B** (picks 201-220 through squad/fixture handoff) -- PASS.
- **Stage C** (Opening Round / Round 1 weekly lifecycle) -- PASS, with one
  disposable-checkpoint setup correction (not an application defect).
- **Stage D** (first-half closeout -> 9-to-20 continuation -> Round 10 ->
  mid-season handoff) -- PASS, including a genuine fail-closed acceptance
  case (a misconfigured `main`/`selective` trigger correctly rejected and
  correctable by the Scorer).

Combined with the separate current-code browser acceptance of the
mid-season draft (see above), this satisfies #224's regression requirement
for the timeline it actually covers: preseason through the Round 10 ->
mid-season handoff. The second-half (Rounds 11-20) and Finals/SuperScore
evidence remains proven only at each phase's own historical baseline, not
current code -- see the readiness document's Stage A-D scope note, which
this summary does not restate or override.

## Replay-evidence versus automated-test-only conclusions

This summary and the phase documents it draws on describe what the
**replay actually exercised against real persisted state**, through the
browser or CLI as noted above. Conclusions here are replay evidence.
Where a capability is only proven by `pytest` (unit/integration tests with
no operator driving a browser or CLI session), the readiness document
labels it **automated-test-proven** instead, and where a capability has
had no replay or acceptance exercise at all, it is labelled **staging/
rehearsal-needed** or **outstanding**. This document does not itself make
that classification for every domain; see the readiness document's matrix.

## Non-goals preserved

Consistent with #224: this document does not collapse or delete the three
phase evidence directories, does not rewrite any historical evidence to
imply current code existed at the time, and does not claim any preserved
replay source database was mutated for the current-code regression -- every
regression stage above ran against a disposable, migrated *copy*.
