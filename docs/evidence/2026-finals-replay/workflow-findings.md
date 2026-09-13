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
