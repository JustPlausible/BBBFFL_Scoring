# Finals/SuperScore replay: workflow findings

Durable findings discovered while issue #194 prepared this phase's operator
surface, in the same spirit as
[`2026-second-half-replay/workflow-findings.md`](../2026-second-half-replay/workflow-findings.md).
These are findings about the *tooling/code*, not about replaying finals or
SuperScore against real historical data -- no such replay has run yet (see
[`README.md`](README.md)'s "Status" section).

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

No other defect was found in #190-#193/#195 while preparing this phase's
tooling.

## Not yet found: any finding from actually running the phase

Everything above was found while building the operator surface, before any
finals week or SuperScore round has been played against the real 2026
replay database (see `README.md`). Genuine replay-execution findings
(historical-data availability, workbook discrepancies, UX friction) belong
in a `round-results.md`/`ux-findings.md` this directory does not yet have --
create them, in the same shape as the second-half replay's own documents,
when that execution actually happens.
