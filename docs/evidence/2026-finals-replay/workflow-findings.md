# Finals/SuperScore replay: workflow findings

Durable findings discovered while issue #194 prepared this phase's operator
surface, in the same spirit as
[`2026-second-half-replay/workflow-findings.md`](../2026-second-half-replay/workflow-findings.md).
Findings 1-9 were discovered while preparing the tooling/operator surface. The execution findings added below were observed while subsequently running the full 2026 Finals/SuperScore historical replay through Grand Final/SS4 and season closeout.

## Finding 1: SuperScore round setup/review and season completion had no operator-reachable entry point

**Severity:** blocking for this issue's own acceptance criteria (a playbook
must reference "the actual current operator surface", and none existed for
these three domains).

Issues #192, #193 and #195 fully implemented and tested (in
`tests/test_superscore_round.py`, `tests/test_season_completion*.py`, etc.)
the following domain logic, but none of it was reachable from outside a
test file -- no HTTP route, no CLI script, unlike `app.finals`/`app.
finals_seeding`, which both received a CLI in issues #190/#187:

- `app.superscore_round`: `ensure_stream`, `ensure_round`,
  `confirm_afl_mapping`, `setup_round`, `open_round`.
- `app.superscore_review.SuperScoreReviewRepository`: `record_dnp_ruling`,
  `record_interchange_ruling`, `record_override` -- the entry-scoped
  counterpart of the matchup-keyed DNP/Interchange/override routes
  `app/routes/round_review.py` already exposes for ordinary and finals.
- `app.season_completion`: `preview_complete_season`, `complete_season`.

**Why this belongs in #194, not a reopened #192/#193/#195:** those issues'
PRs are already merged and closed. This gap sits squarely in #194's own
mandate -- "write the operational playbook ... referencing #190-#193's
actual CLI/API surface (not hypothetical commands)" is not satisfiable
while three domains have no real surface to reference. Closing it required
zero changes to `app.superscore_round`/`app.superscore_review`/`app.
season_completion`'s domain logic (aside from finding 2 below) -- three new
CLI scripts (`scripts/superscore_round_2026.py`,
`scripts/superscore_review_2026.py`, `scripts/season_completion_2026.py`),
mirroring `scripts/finals_bracket_2026.py`/`scripts/finals_seeding_2026.py`'s
established preview/apply, `--reason`-required, production-guarded shape,
close it.

## Finding 2: `app.superscore_round`'s review-state completeness check rejected real PostgreSQL

**Severity:** would have made the new SuperScore round-setup CLI (finding
1) non-functional against the only database engine this replay ever uses.

`app.superscore_round._create_review_state_rows` verified the just-inserted
review-state row count with:

```sql
SELECT COUNT(*) AS n FROM superscore_entry_review_state WHERE bbbffl_round_id=? FOR UPDATE
```

Real PostgreSQL rejects `FOR UPDATE` combined with an aggregate function
("FOR UPDATE is not allowed with aggregate functions"). This was a
pre-existing defect in issue #192's own code, confirmed present on the
unmodified base branch, and unrelated to issue #195's write fence -- a
prior session working on issue #195 had already discovered it and worked
around it with a test-only helper
(`tests/season_completion_helpers.py::
_setup_round_without_the_pre_existing_postgres_count_for_update_bug`)
rather than fix `app.superscore_round` itself, since #195's own scope was
explicitly restricted to "adding the shared write-fence guard".

Issue #194 fixed it directly: the completeness check now locks and counts
the individual rows in Python (`SELECT season_entry_id ... FOR UPDATE`,
`len(rows)`) instead of `COUNT(*) ... FOR UPDATE` -- identical locking
guarantee, no PostgreSQL incompatibility. The test helper above was
simplified to delegate to the now-fixed real `setup_round` instead of
duplicating its logic. SQLite's own test suite never caught this because
SQLite tolerates `FOR UPDATE` on an aggregate query silently; this is
exactly the kind of PostgreSQL-only defect
`tests/test_season_completion_postgresql.py`-style real-Postgres regression
tests exist to catch, and this issue relies on those same tests (unchanged)
continuing to exercise the fixed code path.

This is the one, narrow, necessary code change issue #194 made outside its
own new files -- consistent with the issue's own safety boundary ("this
issue documents and operationally exercises #190-#193/#195's actual
behaviour... If running this phase's real operator procedure surfaces a
genuine defect... determine whether a small, necessary fix is appropriate").

## Finding 3: `app.superscore_review` had no completed-season write fence

**Severity:** blocking — would have let the new SuperScore review-ruling
CLI (finding 1) silently mutate review state after issue #195's completion
transaction, undermining #194's own step-7 archival guard.

Found by Codex review (P1) on PR #207. `app.superscore_review.
SuperScoreReviewRepository.record_dnp_ruling`/`record_interchange_ruling`/
`record_override` had no `app.season.SeasonRepository.guard_writable` call
at all, unlike `app.superscore_results`/`app.finals_review`/`app.
calculations`, which all take that lock first in their own write
transactions (issue #195's shared completed-season write fence). This was
a genuine, pre-existing gap in `app.superscore_review` (issue #192) that
had no consequence in practice only because nothing outside test code
could reach these methods before this issue's new
`scripts/superscore_review_2026.py` CLI existed.

Fixed directly in `app.superscore_review` (a `_guard_season_writable`
helper, called first in each of the three write transactions, resolving
`season_id` via `bbbffl_round_lifecycle`) — the same small, necessary,
already-established pattern, not a new mechanism. Regression tests in
`tests/test_superscore_round.py` and `tests/test_superscore_review_cli.py`
prove all three methods now raise `SeasonCompletedError` once
`complete_season` has run.

## Finding 4: the SuperScore/finals AFL-round-mapping CLI trusted an operator-suppliable identifier

**Severity:** correctness/replay-integrity — could have let a SuperScore
round score against the wrong real AFL round despite the confirmed
SS1-SS4/finals-week concurrency invariant, with nothing catching it.

Found by Codex review across two further rounds on PR #207, each
deepening the previous fix. `scripts/superscore_round_2026.py`'s original
`confirm-mapping` accepted raw `--afl-season-id`/`--afl-round-id` from the
operator with only `AflApiReferenceValidator.round_exists` (proves the
pair exists *somewhere* in afl-api evidence, not that it's the *correct*
pair) checking it. The first fix added `--finals-round-id` to derive/
cross-check against a named finals round's own accepted mapping — but
that identifier was itself still operator-suppliable and unverified: an
operator could name a real finals round for the *wrong* week (or a
different season entirely) and the CLI would accept it.

The fix that actually closes this is removing every operator-suppliable
"which finals round" parameter from the recommended path.
`app.superscore_round.resolve_concurrent_finals_afl_mapping` (new)
derives both the owning season and the exact required week number
directly from the SuperScore round's own `round_key`/season — nothing
left for an operator to get wrong, because nothing is asked. This lives
in `app.superscore_round` itself (not just the CLI), so any future caller
gets the same guarantee. 5 new domain-level tests
(`tests/test_superscore_round.py`) cover the derivation and every failure
mode, including the exact "SS1 given week 2's round" scenario the review
named.

**A fourth review round found this still wasn't quite enough**: the CLI
still let an operator supply `--afl-season-id`/`--afl-round-id` explicitly,
bypassing the new derivation entirely and reopening the identical
mistyped-but-real-AFL-round risk. Since every round this 2026-specific
CLI ever handles genuinely has the finals-concurrency invariant, there is
no legitimate use for an override — it was removed from
`scripts/superscore_round_2026.py` entirely rather than validated, closing
this by construction (no flag left to misuse) instead of by another check.

## Finding 5: `setup-round`/`open-round` accepted any round, including a finals week's

**Severity:** operator-safety — could have let `setup-round`/`open-round`
run SuperScore's own lifecycle transitions against a finals-week round,
bypassing its proper `open_finals_week` pairing-materialisation/preflight
pathway entirely.

Found by the same fourth Codex review round on PR #207.
`CompetitionLifecycleRepository.create_non_ordinary_round` (#197) permits
both finals and SuperScore streams, so nothing stopped `app.
superscore_round.setup_round`/`open_round` from being called against a
finals round by an operator mistake (e.g. a copy-pasted round id). Fixed
with a new `_require_superscore_round` guard in `app.superscore_round`,
called first in both functions — refuses (`SuperScoreRoundError`, no
mutation) a round that isn't `superscore`-typed. Fixed at the domain
layer, not just the CLI, so any future caller gets the same guarantee.

## Finding 6: `ensure_round`/`resolve_concurrent_finals_afl_mapping` still accepted a non-SuperScore competition/round

**Severity:** operator-safety — the same class of defect as finding 5, one
step earlier in the pipeline: could have let a bogus `ss1`-`ss4`-labelled
round be created and mapped under a finals competition before finding 5's
guard (in `setup_round`/`open_round`) ever ran.

Found by a fifth Codex review round on PR #207. Finding 5's guard fenced
`setup_round`/`open_round`, but `ensure_round` itself still accepted any
`--competition-id`, including a finals one — `resolve_concurrent_finals_
afl_mapping` likewise selected only `round_key`/season, not
`c.stream_type`, so a round planted under the wrong stream (via `ensure_
round`'s bug, or by going directly through `SeasonRepository.create_round`)
would still have its mapping happily derived and accepted. Fixed with
`_require_superscore_competition` in `ensure_round` (refuses before any
round row is created) and the same `stream_type` check added to `resolve_
concurrent_finals_afl_mapping`'s own initial query (refuses even a round
planted directly, bypassing `ensure_round` entirely) — closing the gap at
creation time, not only at `setup_round`/`open_round`. Domain-level tests:
`tests/test_superscore_round.py::test_ensure_round_refuses_a_non_
superscore_competition_id`, `::test_resolve_concurrent_finals_afl_mapping_
refuses_a_round_planted_under_the_wrong_stream`.

## Finding 7: the playbook ran each finals week to full publication before that round's SuperScore lineups were ever submitted

**Severity:** correctness — SuperScore could never have been legitimately
played at all, since the lockout guard would reject every SS lineup for a
round whose shared AFL checkpoint had already advanced to final results.

Found by the same fifth Codex review round. An earlier playbook draft ran
all four finals weeks to full publication (section D, as it then read)
before ever asking coaches to submit SuperScore lineups (section E, as it
then read) — but finals and SuperScore share AFL rounds 21-24
(`docs/2026-finals-superscore-design.md`'s confirmed rule), and publishing
a finals week requires its shared AFL round's replay checkpoint to have
advanced to final results. Once that has happened, `CoachLineupService.
submit`'s lockout guard treats the round as already played, and a first
SuperScore submission filling previously empty positions is rejected as
locked. This is a finding about the playbook's own procedure, not the
underlying domain code (nothing in `app.lineups`/`app.lockouts` needed to
change — the lockout guard is doing exactly its job). Fixed by merging
the old sections D and E into one interleaved per-round procedure: both
streams' round `N` are created/opened together, both streams' lineups for
round `N` are submitted while the shared AFL round is still open, and only
then does either stream finalise.

## Finding 8: nothing exposed the SuperScore/finals lifecycle's `open -> review` transition, and the playbook never released final AFL evidence before publishing

**Severity:** blocking — walking the (now-fixed, finding 7) interleaved
per-round procedure against the real merged code showed both finals
publication and SuperScore calculation would fail at the first round: two
distinct, compounding gaps, both found by a sixth Codex review round on
PR #207.

1. **No stream-aware `open -> live -> review` transition existed.**
   `publish_finals_round` requires a finals round to already be in
   `review` (`app/finals_review.py`); `SuperScoreLeaderboardService.
   _persist` requires `review` or `final` (`app/superscore_results.py`).
   But `open_finals_week`/`open_round` only ever transition a round to
   `open`, and the one HTTP route that can advance further
   (`/rounds/{id}/transition` in `app/routes/round_review.py`) explicitly
   refuses any non-`ordinary` round, directing it to "its own
   stream-aware lifecycle module" — which, until this finding, did not
   exist for finals or SuperScore. Fixed with `app.finals.
   FinalsBracketRepository.advance_week_to_review` and `app.
   superscore_round.advance_round_to_review`, both thin, stream-guarded
   wrappers around the same, already-tested `CompetitionLifecycleRepository.
   transition`/`LEGAL_TRANSITIONS` every ordinary round already uses — no
   new lifecycle mechanism — exposed via new `advance-week-to-review`/
   `advance-to-review` CLI subcommands. Domain tests: `tests/test_finals.py`
   (`test_advance_week_to_review_moves_an_open_week_through_live_to_review`,
   `test_advance_week_to_review_refuses_a_week_that_is_not_open_yet`),
   `tests/test_superscore_round.py`
   (`test_advance_round_to_review_moves_an_open_round_through_live_to_review`,
   `test_advance_round_to_review_refuses_a_round_that_is_not_open_yet`,
   `test_advance_round_to_review_refuses_a_finals_round`); CLI wiring
   covered in `tests/test_finals_cli.py`/`tests/test_superscore_round_cli.py`.
2. **The playbook's interleaved procedure (finding 7) never released the
   shared AFL round's final evidence.** `ReplayAflDataSource.
   get_match_player_stats` reports a round's final statistics only once
   the replay checkpoint has been advanced past it (`--stage
   final-results --round-id <id>`, the same mechanism
   `2026-first-half-replay-playbook.md` section G step 9 already
   documents and this replay already reuses for the ordinary Rounds
   10-20 loop) — the finals/SuperScore draft simply never called it. This
   is a playbook-only omission, not a code defect: the checkpoint
   mechanism itself is unchanged and reused, exactly per this issue's own
   safety boundary against inventing a new backup/checkpoint mechanism.

Both are fixed together in the playbook's per-round loop (section D.3):
step (e) releases the shared round's final evidence and restarts the app;
step (f) advances both streams' round `N` to `review`; publication (steps
g-h) follows only after both.

## Finding 9: SuperScore's mapping derivation read the mapping's mutable current head, not the finals week's frozen mapping

**Severity:** correctness/replay-integrity — a genuine, if narrow, race
between SuperScore's `confirm-mapping` and an out-of-band correction to
the concurrent finals week's mapping could still have violated the
shared-round invariant findings 4/6 otherwise closed off.

Found by a seventh Codex review round on PR #207, after the playbook's
per-round procedure (finding 7) and lifecycle-transition fix (finding 8)
made walking a full round genuinely possible for the first time.
`app.competition_lifecycle.CompetitionLifecycleRepository.
create_non_ordinary_round` freezes `mapping_id`/`mapping_revision`/
`afl_season_id`/`afl_round_id` onto a round's own `bbbffl_round_lifecycle`
row at *round-creation* time (called by `open_finals_week` the first time
a finals week opens); every later read of that round for calculation
(`app.calculations._round_context`) reads that frozen snapshot, never a
fresh lookup. But `app.superscore_round.resolve_concurrent_finals_afl_
mapping` (finding 4's fix) called `RoundMappingRepository.resolve()`,
which returns the mapping's *current* accepted revision — and `app.
round_mapping.RoundMappingRepository.correct` has no dependency on
lifecycle state at all, so a correction to a finals week's mapping after
that week has already opened succeeds without updating its
already-frozen lifecycle row. A SuperScore `confirm-mapping` running
between such a correction and any awareness of it would derive the
*corrected* mapping, while the concurrent finals week's own calculations
kept using the *original, frozen* one — the two streams silently scoring
against different real AFL rounds despite the confirmed concurrency
invariant.

Fixed by reading the finals week's own frozen `bbbffl_round_lifecycle`
row directly instead of `RoundMappingRepository.resolve()`'s mutable
head, and refusing (`SuperScoreRoundError`) if that week has not been
opened yet (nothing is frozen to derive from before then) — the same
"derive from an immutable fact, not a value that can change out from
under you" principle findings 4 and 6 already established, applied to
which *source* of the mapping is authoritative rather than which
identifier names the round. No new mechanism: `bbbffl_round_lifecycle`
already carried these columns for exactly this purpose (`_validate_
frozen_context`'s own equivalent check at the `upcoming -> open`
transition). Regression test: `tests/test_superscore_round.py::
test_resolves_the_frozen_lifecycle_mapping_not_a_later_correction`
(corrects a finals week's mapping after opening it, then proves
SuperScore still derives the original, frozen AFL round). This also
tightened `resolve_concurrent_finals_afl_mapping`'s precondition: it now
requires the finals week to have been *opened* (not merely mapped), which
was already the playbook's own step order (open finals week `N` before
SS`N`'s `confirm-mapping`, per finding 7's interleaved procedure) — no
playbook step reordering was needed, only its explanatory note.

No other defect was found in #190-#193/#195 while preparing this phase's
tooling.

## Finding 10: the replay season remained in `setup` until closeout

**Severity:** closeout blocker / future live-season lifecycle gap.

The first real `scripts.season_completion_2026 preview` correctly refused because the replay season was still `setup`. The replay bootstrap had intentionally created the season in `setup`, but no later replay operation had transitioned it to `active`; nevertheless the entire ordinary season, mid-season draft, Finals and SuperScore workflows had been able to proceed.

For the historical replay closeout, the operator used the supported audited `SeasonRepository.transition_lifecycle(..., "active")` path with an explicit replay-closeout reason. The season moved to version 2, and the next completion preview returned `ready: true` with all eight Finals/SuperScore rounds final. The atomic completion then moved the season to `completed`, version 3.

**2027 implication:** season activation needs to be an explicit operational milestone/gate in preseason setup. It should not be possible for a live season to progress indefinitely while the parent season remains `setup`.

## Finding 11: the browser Scorer workflow was substantially easier than the original CLI-heavy playbook, but navigation still matters

The real replay increasingly used the Scorer web surfaces rather than the original command-by-command CLI procedure. The browser workflow successfully carried the operator through Finals progression, paired Finals/SuperScore operation, review and publication. The remaining friction was primarily discoverability: a correct "Complete Grand Final preflight" blocker did not initially give a human operator an obvious route to the relevant Finals preflight page, while the preflight index stopped at Round 20. This is tracked by issue #221.

The replay therefore supports retaining CLI tools as recovery/operator primitives while treating the browser Scorer workflow as the normal 2027 path. A non-coding Scorer beta rehearsal remains desirable before live use.

## Finding 12: real Coach and exceptional Scorer paths were exercised during Finals

The execution phase exercised more than the happy path:

- two authenticated Coach accounts were retained through the final two replay rounds;
- Finals eligibility and cross-coach private-lineup isolation were verified;
- Finals and concurrent SuperScore submissions remained independent;
- a non-finalist Coach could participate in SuperScore without gaining Finals lineup access;
- delegated lineup entry, missed-submission adjudication and locked-lineup correction were all used;
- DNP/Interchange review, calculation, finalisation and publication were exercised;
- a Preliminary Final with a main-only lockout plan proved no synthetic early trigger is required.

These are positive operational findings, not merely unit-test coverage.

## Finding 13: remaining UX follow-up identified by execution

The replay exposed several non-blocking 2027 UX improvements:

- Coach Account weekly selections should be ordered by competition chronology rather than colliding ordinary/Finals/SuperScore sequence numbers.
- Coaches participating in both Finals and SuperScore would benefit from an optional same-week "copy other lineup to draft" action; copying must never bind or auto-submit the two independent lineups.
- Carry-forward plus an existing private draft can make the authoritative submitted lineup difficult to understand in the Scorer UI; submitted state versus private changes should be clearer.
- The Scorer Dashboard's explanatory workflow cards and Finals-preflight handoff need small presentation/navigation polish (issue #221 covers the current preflight/dashboard work).

None blocked completion of the historical replay, but they are useful candidates for the post-replay 2027 UX backlog.

## Finding 14: season completion and archival guard succeeded against the real replay

After the lifecycle repair in finding 10, completion preview returned ready with Finals Weeks 1-4 and SS1-SS4 all final. The atomic completion transaction succeeded, created the Premiership and Wooden Spoon season awards, and transitioned the season to `completed` version 3. The independent archival guard then re-derived and matched the exact `season.completed` event before the final paired database/checkpoint archive was taken. The dump passed `pg_restore --list` and both private checksums verified.

The sanitised identifiers and timestamps are recorded in `provenance-manifest.md`; private backup filenames and hashes remain outside GitHub by policy.

## Finding 15: completed-season write fence passed a real post-closeout smoke test

After the final archival checkpoint was taken, the operator ran a read-only SS4 review status for JHAS and observed review version 1 with no DNP rulings, no interchange ruling and no overrides. An attempted SS4 DNP mutation against that exact current review version was then made through the supported `scripts.superscore_review_2026` CLI.

The application refused the operation with the completed-season fence:

`season 3832745c-c19a-4224-bceb-86ded6baa09c is completed; result-changing operations are permanently refused`.

A second read-only status immediately afterwards still reported review version 1 with empty slot rulings, no interchange ruling and no overrides. This is direct operational evidence that the completed-season fence rejected the supported result-changing path without mutating review state.

The archival verifier was then re-run after the refused mutation and again returned completed season version 3 with the same completion event `9cd65eee-d6ec-43b5-bcb7-23b275ac227a` (verified at 2026-09-19T09:46:21.433457+00:00), confirming the archival identity remained unchanged after the failed write attempt.
