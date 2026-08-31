# 2026 Rounds 1-9 replay checkpoint (issue #67)

Issue #66 built a deterministic one-round replay harness and proved it on an
*ordinary* round. Before attempting the real, sequential 2026 Rounds 1-9
replay, issue #67 adds a small, deliberately explicit **checkpoint suite**
that exercises the difficult cases the real replay will actually hit:
staged/early AFL-match lockouts, ordinary AFL bye/availability warnings,
missing or ambiguous historical submissions (carry-forward and scorer/proxy
entry, including the genuine "no prior lineup" case), and Opening Round
deferred/compensating-bye mixed-source scoring (issue #69).

This is a **replay-validation and evidence/provenance exercise**, not a
second scoring engine. Every scenario below drives the same production
repositories/services #66 and #69 already implement --
`app.lockouts`, `app.lineup_validation`, `app.carry_forward`,
`app.lineup_proxy`, `app.opening_round`, `app.calculations`,
`app.round_review`, `app.ladder` -- through `app.replay.ReplayAflDataSource`/
`ReplayClock` for controlled evidence and time. `app.replay_checkpoint`
contributes no sporting rule of its own; it only shapes each scenario's
already-computed results into one deterministic, comparable report. See
`docs/replay-harness.md` for the shared replay evidence/report conventions
this checkpoint reuses (evidence schema, provenance classification,
`ReplayClock`, `write_replay_report`'s sibling `write_checkpoint_suite_report`),
and that document's "Three distinct checkpoints" section for how this
hermetic checkpoint suite, issue #85's persistent interactive Round 1
rehearsal, and the historical Rounds 1-9 replay this document's own
"Operator procedure" section below describes relate to (and do not
substitute for) one another.

## Running the checkpoint suite

```bash
cd bbbffl_app
pytest -q tests/test_replay_checkpoint.py
```

Every scenario constructs its own isolated, migrated, in-memory database
(`tests.db_helpers.migrated_connection`), loads its own evidence file from
`tests/fixtures/replay_checkpoint_2026/`, and is proven deterministic by a
dedicated `..._is_deterministic_on_rerun` test that runs the scenario twice
from clean state and asserts the reports are equal. A final pair of tests
(`test_checkpoint_suite_aggregates_every_required_scenario`,
`test_checkpoint_suite_is_deterministic_on_full_clean_rerun`) assembles all
five scenario reports into one suite via `app.replay_checkpoint.
build_checkpoint_suite` and proves the whole suite reruns identically.

## Scenarios

Each scenario reports one of `PASS`, `FAIL`, or `UNRESOLVED`
(`app.replay_checkpoint.ScenarioOutcome`). An outcome is computed
automatically from the scenario's own `unresolved_questions`/
`discrepancies`: any unresolved question makes the outcome `UNRESOLVED`
first (never a silent pass); otherwise any discrepancy makes it `FAIL`;
otherwise `PASS`.

| Scenario ID | Historical/synthetic | Outcome | Fixture |
|---|---|---|---|
| `early_lockout` | synthetic (AFL round identity is a known 2026 fact) | PASS | `early_lockout_evidence.json` |
| `bye_availability` | synthetic | PASS | `bye_availability_evidence.json` |
| `round1_no_prior_lineup` | synthetic | **UNRESOLVED** (deliberately) | none (direct `app.carry_forward` construction) |
| `carry_forward_and_proxy_provenance` | synthetic | PASS | none (direct `app.carry_forward`/`app.lineup_proxy` construction) |
| `opening_round_deferred_mixed_source` | synthetic (Opening Round/compensating-bye identity is a known 2026 fact per `docs/opening-round-deferred-selection.md`) | PASS | `opening_round_deferred_evidence.json` |

No historical BBBFFL coach submission, nomination, or lineup record exists
in this repository for any specific 2026 round (see
`docs/opening-round-deferred-selection.md`'s "Unresolved historical BBBFFL
mappings" section, which this checkpoint deliberately does not contradict).
Every scenario is therefore an explicitly labelled synthetic scenario built
to exercise the mechanism deterministically; only specific AFL-side facts
within them (the 2026 AFL round identities, and Brisbane Lions' Opening
Round/compensating-bye mapping) are `known_fact`. This mirrors the four-way
evidence classification `app.replay.EvidenceClass` and each fixture's
per-record `provenance` already carry: `known_fact`,
`reconstructable_behaviour`, `synthetic_scenario`, `unresolved_scorer_input`.
`round1_no_prior_lineup`'s own carry-forward-source classification is
recorded as `unresolved_scorer_input` -- the scenario's entire point.

### `early_lockout` -- staged/early lockout

A BBBFFL round configured with two `app.lockouts.LockoutTriggerRepository`
triggers: a Thursday **selective** stage covering one AFL match, and a
Saturday **main** stage covering a second. Nine slots are submitted: F1
resolves to the Thursday match, every other slot (including Interchange) to
the Saturday match. Three controlled `ReplayClock` instants are evaluated:
before either trigger fires (everything editable), between the two
(F1 locked, everything else still editable), and after both (everything
locked). The test does not merely inspect a computed `locked=true` flag --
it *attempts* the prohibited mutations through
`WeeklyLineupRepository.submit_positions`:

- mutating the now-locked F1 fails with `LockedSelectionError`;
- routing a locked-match player through Interchange instead also fails with
  `LockedSelectionError` (Interchange cannot bypass a lock boundary);
- a permitted edit of the still-editable M1 succeeds and produces a new
  immutable submission version, while the original version-1 submission
  (read back via `get_submission`) is untouched;
- after the main trigger activates, the position that was editable a
  moment ago is rejected too.

Lock diagnostics (`LockoutRepository.lock_state`) expose the effective
replay time, the AFL match identity, the affected slot, and the lock
reason (`selective_trigger_activated` / `main_lockout_triggered`) at each
checkpoint -- see the scenario's `lockout` report section.

### `bye_availability` -- ordinary bye vs. genuinely ambiguous evidence

One BBBFFL round with two distinguishable cases in the same lineup: F1's
club has an ordinary scheduled AFL bye (`round.byes` in the evidence file);
Ruck's club played, but the evidence deliberately has no afl-api stat row
for that player. `LineupValidationService.validate_submission` reports F1's
bye as a `category="availability", code="afl_club_bye"` warning
(`dnp: False`) and nothing else -- the lineup remains otherwise valid, the
system never substitutes or "optimises" the player. `app.participation.
assess_participation` independently confirms `club_bye` /
`dnp_recommendation="not_dnp"`, so no automatic DNP is ever created for an
ordinary bye. Ruck, by contrast, genuinely blocks `app.round_review.
build_round_review`'s sign-off readiness until an explicit scorer ruling
(`record_dnp_ruling` + `record_interchange_ruling`) is recorded -- proving
the "genuinely exceptional case requiring human judgment" path stays
distinct from an ordinary bye and stays explicit scorer input.

### `round1_no_prior_lineup` -- deliberately unresolved

Round 1 of a fresh competition stream, one entry, no submission at all.
`CarryForwardService.resolve_source` returns `None` and `carry_forward()`
raises `NoCarryForwardSourceError` -- never inventing a default/optimised
team. The scenario's report carries one `unresolved_questions` entry and
its outcome is `UNRESOLVED`: a missing answer is itself the valid replay
finding here, and the checkpoint machinery refuses to let an unresolved
scenario silently read as `PASS`
(`app.replay_checkpoint._validate_scenario` asserts this invariant).

### `carry_forward_and_proxy_provenance` -- exact copy vs. proxy entry

A second round in the same stream. One entry's round-1 coach submission is
carried forward **exactly** into round 2 (no optimisation, no hindsight);
the report's `carry_forward` section records the source round/version and
confirms `positions_match_source_exactly`. A second entry has no
recoverable round-1 evidence at all, so a scorer/admin enters its round-2
lineup through `app.lineup_proxy.LineupProxyService` instead; the report's
`proxy_entry` section records the operator actor/role and
`source_type="scorer_proxy"` -- distinguishable from `"coach"` in both
cases, never fabricated as an authenticated coach submission.

### `opening_round_deferred_mixed_source` -- issue #69 in one replay lineup

One BBBFFL round (mapped to 2026 AFL round 1345, R2) where M2 scores
ordinarily from that mapped round while M1 -- a slot nominated earlier from
AFL Opening Round (round 1343) for a club whose compensating bye lands in
this round -- scores from its own frozen Opening Round match. Both AFL
rounds' matches/stats come from the *same* `ReplayAflDataSource`, keyed by
AFL round/match ID rather than by BBBFFL round, which is exactly what lets
one `MatchupCalculationService.calculate_matchup` call mix two AFL source
rounds unambiguously. The report's `deferred_source` section exposes the
nominated player, target slot, nomination provenance, source AFL round,
source AFL match, source evidence classification, the current BBBFFL
scoring round, and an explicit explanation string; `calculated_result`
confirms M1's `scoring_source == "opening_round_deferred"` against AFL
round 1343 while M2's is `"ordinary"` against AFL round 1345. An attempted
edit of the locked deferred slot (`OpeningRoundSelectionGuard`) fails with
`DeferredSlotLockedError`, proving the nomination stays locked under #69's
own rules through the replay path, not a replay-only shortcut.

## Determinism

Every scenario builds its own database from a clean
`tests.db_helpers.migrated_connection()`, its own `ReplayAflDataSource`
load, and explicit `ReplayClock` instants -- never current wall-clock time,
live AFL API responses, browser state, or leftover database rows. Every
report converts internal randomly-generated identifiers (season player
IDs, lineup IDs, round IDs) to their stable `canonical_player_id` or a
fixed round label before comparison, so the `..._is_deterministic_on_rerun`
tests compare on evidence-stable identity, not on incidental UUIDs.

## What this checkpoint does not do

Per issue #67's scope: no replay-only scoring path (every score comes from
`app.calculations.MatchupCalculationService`); no simulation of finals or
SuperScore; no post-Round-9 trade/delist/mid-season-draft transition; no
claim that this checkpoint *is* the full 2026 Rounds 1-9 replay -- it is
the validation gate before attempting it. Any prerequisite this checkpoint
found missing would be reported as a defect against #66/#69 rather than
worked around here; none was found missing during this implementation.

## Operator procedure: the real 2026 Rounds 1-9 sequential replay

This section is for the human operator performing the actual first replay
checkpoint after issue #67 lands: 2026 Rounds 1 through 9, **sequentially**,
inspecting the ladder after every finalised round. It does not cover
finals, SuperScore, or anything after Round 9's post-season transition.

**Before you start:** do not alter fixtures or historical evidence merely
to force a match between expected and replayed results. A discrepancy is a
legitimate *output* of the replay process, not a defect in the replay
itself -- record it and, where appropriate, open a follow-up issue. Never
edit a captured match/stat/lineup record just to make a discrepancy
disappear.

1. **Initialise a clean replay database/state.** Start from a freshly
   migrated database (see `bbbffl_app/tests/db_helpers.migrated_connection`
   for the pattern, or the equivalent production migration path) with no
   leftover rows from a previous attempt. Record the migration revision.
2. **Load the deterministic AFL evidence.** Set
   `BBBFFL_AFL_MODE=replay` and `BBBFFL_AFL_REPLAY_EVIDENCE_PATH` to the
   evidence manifest covering AFL rounds 1-9 (`docs/replay-harness.md`'s
   evidence schema/provenance rules apply). Every season, round, match,
   player, and stat line needs its own `provenance` classification; do not
   substitute a live `afl-api` call at any point in a replay run.
3. **Establish the replay clock.** Decide the `ReplayClock` instant(s) you
   will evaluate each round at (pre-lockout, post-lockout, post-conclusion,
   as in `tests/test_replay_round.py`). Never fall back to wall-clock time.
4. **Begin Round 1.** Open the round through the normal competition
   lifecycle (`app.competition.CompetitionLifecycleRepository`). Round 1
   has no predecessor: any entry with no recoverable submission becomes an
   `unresolved_questions` entry (see `round1_no_prior_lineup` above),
   never an invented lineup.
5. **Load/enter each historical BBBFFL lineup using the correct
   provenance.** For each entry, decide which of these applies and use the
   matching mechanism -- never blend them:
   - a genuine historical coach submission you can evidence (`source_type=
     "coach"`);
   - an exact carry-forward from the entry's previous submitted lineup
     (`app.carry_forward.CarryForwardService`, `source_type="carry_forward"`);
   - a scorer/admin proxy entry where no coach evidence is recoverable
     (`app.lineup_proxy.LineupProxyService`, `source_type="scorer_proxy"`).
6. **Distinguish coach submissions, carry-forward, and scorer/proxy entry**
   explicitly in your own operator notes for each entry/round, matching the
   `source_type` actually recorded -- this is what later lets a discrepancy
   be traced to "this was a guess" versus "this was evidenced."
7. **Apply Opening Round deferred nominations where required.** Before
   scoring a round that is a club's configured compensating-bye round,
   confirm any accepted `app.opening_round.OpeningRoundRuleRepository` rule
   and nomination exists and is preloaded
   (`OpeningRoundNominationRepository.preload_target_lineup`). Do not guess
   a nomination that is not independently evidenced; leave it unresolved
   otherwise.
8. **Advance through staged AFL lock times.** Evaluate lockout at each
   configured trigger's boundary (`app.lockouts.LockoutRepository.
   lock_state`/`guard`) using the replay clock, never wall-clock time.
   Confirm early/selective stages lock only their configured matches and
   the main stage locks everything remaining.
9. **Inspect warnings and unresolved scorer inputs.** Run
   `LineupValidationService.validate_submission` for every lineup; note
   every `afl_club_bye` warning (informational, not a DNP) and every
   `unknown`/ambiguous evidence warning that will need a ruling.
10. **Resolve only those inputs for which you have legitimate evidence/
    decision authority.** An `app.round_review` DNP/Interchange ruling
    requires an actual basis (a known unavailability, a confirmed evidence
    gap) -- never resolve a genuinely unknown case just to unblock
    sign-off. Leave it `UNRESOLVED` and record it as an operator note
    instead.
11. **Calculate the round.** Run `app.calculations.
    MatchupCalculationService.calculate_round` against the replay evidence.
12. **Review/sign off/finalise through normal application services.** Use
    `app.round_review.build_round_review`/`attempt_signoff` exactly as a
    live round would. Do not bypass `ready_for_signoff`.
13. **Compare official versus replayed results.** For each matchup, compare
    the newly finalised official result against whatever historical
    official result you have for that round (if any). Record matches and
    mismatches alike.
14. **Inspect and record the ladder immediately after finalisation.** Use
    `app.ladder.LadderRepository.snapshot` (see `docs/ladder-progression.md`)
    right after each round finalises, before moving to the next round --
    this is what makes discrepancies traceable to the round that caused
    them rather than an accumulated drift discovered much later.
15. **Record every discrepancy.** A discrepancy (score, ladder position,
    or otherwise) is an expected *output* of this process, not something to
    suppress. Where a discrepancy indicates a genuine defect (in evidence,
    in a domain service, or in this checkpoint's own assumptions), open a
    follow-up issue describing it precisely -- do not fix it by editing the
    replay evidence.
16. **Proceed to the next round only after the previous round's replay
    state has been intentionally finalised.** Do not pre-load or
    pre-calculate a later round while an earlier round's review is still
    open; the staged-lockout and Opening Round mechanisms both depend on
    each round's state being genuinely settled before the next one begins.

Repeat steps 4-16 for Rounds 2 through 9. At the end, you have nine
finalised rounds, nine ladder snapshots, and a discrepancy log -- the
actual evidence base for deciding whether the 2026 season is ready to be
treated as replay-validated. This procedure, and the checkpoint scenarios
above, do not themselves constitute that full replay; they are what must
pass before it is attempted.

> **Operational scope:** For the authoritative 2026 historical first-half operational replay, use [`2026-first-half-replay-playbook.md`](2026-first-half-replay-playbook.md). This document remains replay-checkpoint-2026 reference material.
