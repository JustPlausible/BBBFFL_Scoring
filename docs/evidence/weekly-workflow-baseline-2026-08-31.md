# Weekly workflow acceptance baseline — 2026-08-31

## Status

**Accepted as the current single-round weekly-workflow baseline.**

This checkpoint records the outcome of three human-operated Round 1 rehearsal passes, including the staged progressive-lockout rehearsal from #91 / PR #96 and the deliberate-vacancy correction from #98 / PR #99.

The final Part 3 rehearsal was performed after #98 was resolved and the operator reported that all recorded events worked as expected as far as can presently be established through human interaction and the current competition-rule assumptions.

The implementation merged by PR #99 is represented on `main` by commit `4d5eb590d5220b8a4fab6e9d3bcf3cd3db41add7`. This document is the human acceptance record associated with that implementation state; a repository tag may be created separately to preserve the exact accepted code reference.

## What this baseline establishes

The following regular-season vertical has been exercised successfully in a controlled one-round rehearsal:

`bootstrap -> coach login -> draft save -> validation -> lineup submission/resubmission -> public/scorer submitted-lineup visibility -> progressive lockout -> calculation -> review -> atomic sign-off -> official public result -> ladder`

### Coach workflow

Human interaction has demonstrated:

- coach authentication and access to the weekly team interface;
- private draft saving;
- authoritative lineup submission and subsequent valid resubmission;
- deliberately vacant positions can be formally submitted after #98;
- the coach receives an explicit confirmation before submitting a lineup containing deliberate vacancies;
- a submitted vacancy is legitimate competition state rather than a fabricated player or DNP;
- submitted teams are visible through the intended scorer/public surfaces while private drafts remain distinct;
- locked selections become visibly non-editable while selections unaffected by the current lock boundary remain editable.

### Progressive lockout

The #91 staged rehearsal was exercised interactively through `initial`, `selective-a`, `selective-b`, and `main` states.

The operator deliberately moved rehearsal players into arbitrary BBBFFL positions before later lock boundaries. This provided useful evidence that locking follows the selected player's AFL-match identity rather than a hard-coded fantasy position:

- Selective A locked Rehearsal Player 1 wherever that player was currently selected;
- Rehearsal Player 1 remained locked while other players could still be rearranged and resubmitted;
- Selective B subsequently locked Rehearsal Players 2 and 3 wherever the coach had moved them;
- previously locked selections remained protected;
- Main lockout left all remaining selections locked.

The coach UI removed/disabled the relevant selectors as players became locked. Existing automated acceptance coverage separately exercises server-side rejection of crafted attempts to alter locked selections.

### Deliberate vacancies and Interchange

The earlier rehearsal exposed that incomplete lineups could be saved privately but could not be formally submitted. #98 documented that this conflicted with two intended BBBFFL workflows:

1. partial early submission before later AFL teams are confirmed; and
2. an intentional vacant ordinary position used as an Interchange opportunity.

PR #99 resolved that validation gap and added a coach-facing confirmation safeguard. The Part 3 human rehearsal was then completed successfully with the corrected behaviour.

This baseline therefore treats an explicit vacancy, a selected player later ruled DNP, and unknown/missing/corrupt lineup data as distinct states.

### Scorer, publication and ladder

The rehearsal evidence has demonstrated:

- the scorer can inspect authoritative submitted teams before calculation;
- all five synthetic Round 1 matchups can be calculated;
- review/sign-off works;
- publication is atomic;
- public official results are displayed;
- the Round 1 ladder is produced from the official results.

Earlier observations where the scorer showed `lineup unavailable` and where `/` served an unsuitable legacy Grand Final surface were fixed and did not remain failures in the later passes.

## Operator/setup evidence

The rehearsal also established useful operational expectations:

- Docker Compose-first operation is the preferred rehearsal path;
- `scripts/` is intentionally absent from the production image, so the documented one-off source-mounted bootstrap arrangement is required;
- replay evidence is eagerly loaded, so advancing staged evidence requires an application restart before the new stage is exercised;
- an early missing-evidence failure was traced to the local `.env` referencing the wrong evidence file and is retained only as historical operator/configuration evidence, not an application defect.

## Superseded findings

The following should not be treated as current baseline defects:

- root `/` leading to the legacy Grand Final prototype;
- scorer `lineup unavailable` before first calculation;
- inability to formally submit deliberate vacancies (resolved by #98 / PR #99);
- an earlier non-clean rehearsal's player-stat-resolution warnings, which did not reproduce in the clean passes.

## Scope and limitations

This is deliberately a **workflow acceptance baseline**, not a declaration that the application is season-ready.

This checkpoint demonstrates that the intended coach -> lockout -> scorer -> official result -> ladder workflow works for a controlled single-round scenario under human operation. It does **not** establish correctness for the historical 2026 season, unusual real-world round mappings, all DNP/Interchange combinations, mid-season operations, finals, SuperScore, or 2027 production operations.

Human acceptance also necessarily relies on the competition rules and assumptions understood at the time of rehearsal. Historical replay may expose cases where those assumptions need to be clarified. Such discoveries should be recorded as new rules/evidence rather than silently changing this baseline.

## Regression policy from this checkpoint

Once accepted, the behaviours above should be considered known-good weekly semantics. Future replay or feature work may intentionally extend them, but an unintentional change to an accepted behaviour should be treated as a regression.

In particular, future changes should preserve:

- private draft versus authoritative submission semantics;
- explicit submitted vacancies;
- player/AFL-match-based progressive locking;
- immutability of already locked selections;
- ability to resubmit still-unlocked selections;
- scorer visibility of authoritative submissions;
- atomic official publication;
- official results feeding ladder state.

## Next validation gate

The next major validation phase is the historical 2026 replay. The recommended first season-level gate is Rounds 1-9 followed by reconciliation of the Round 9 ladder and cumulative matchup state against authoritative historical BBBFFL records.

Historical replay findings should be classified as one of:

- blocking correctness defect;
- missing/clarified competition rule;
- operator/UX improvement;
- historical-data anomaly.

That distinction will help protect this proven weekly workflow while allowing real season evidence to drive the next development work.

## References

- #85 / PR #86 — persistent Round 1 rehearsal/bootstrap and earlier rehearsal reports
- #91 / PR #96 — staged progressive-lockout rehearsal coverage
- #98 / PR #99 — intentional vacant positions and partial submitted lineups
- `docs/staged-lockout-rehearsal.md`
- `docs/lockouts.md`
- `docs/weekly-lineups.md`
- `docs/plans/2027-season-decisions.md`
