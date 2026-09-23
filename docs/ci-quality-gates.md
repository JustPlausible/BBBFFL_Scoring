# CI quality gates

Implements roadmap package **07** (`docs/roadmap/2027-season-roadmap.md`,
milestone A) / issue #39: turn CI into an attributable, hermetic quality gate
for the growing 2027 system while preserving the existing pytest and
container-build coverage.

## Required gates and what each one attributes a failure to

`.github/workflows/ci.yml` runs these as separate jobs so a red check names
exactly one concern -- there is no combined "build" job a contributor has to
dig through to find out what actually broke:

| Job | Attributes failures to | Hermetic? |
|---|---|---|
| `test` | the Python/pytest suite | Yes -- no network, no external service |
| `lint` | formatting or the narrow lint rule set (Ruff) | Yes |
| `typecheck` | the incremental mypy scope (see below) | Yes |
| `dependency-audit` | a known, unsuppressed dependency vulnerability | Yes -- queries the public advisory index only, no secrets |
| `postgres-migrations` | migration upgrade/rollback/repository integrity | Yes -- spins up its own disposable PostgreSQL service container |
| `docker-build` | the application container image failing to build | Yes |

All six run on every pull request and on push to `main`, from a clean
`actions/checkout`, with no dependency on a developer's local files, a
running `afl-api`, production credentials, or any state left over from a
previous run. None of them requires network access to anything except PyPI
(for `pip install`) and the public vulnerability advisory index
(`dependency-audit`) -- neither is `afl-api`, and neither needs a secret.

Live/credentialed diagnostics are handled entirely separately -- see
[Hermetic vs. credentialed diagnostics](#hermetic-vs-credentialed-diagnostics)
below.

## Formatting and lint (Ruff)

**Tool:** [Ruff](https://docs.astral.sh/ruff/), configured in
`bbbffl_app/pyproject.toml`'s `[tool.ruff]` / `[tool.ruff.lint]` sections. One
tool provides both the formatter and the linter, rather than combining
several overlapping ones (e.g. Black + isort + flake8).

**Reproduce locally:**

```bash
cd bbbffl_app
ruff format --check .   # formatting gate
ruff check .             # lint gate
```

`ruff format .` (without `--check`) applies the formatting; `ruff check --fix
.` applies the lint autofixes.

**Line length is 120, not Ruff's 88-column default.** This codebase already
had its own wrapping style before Ruff was adopted; 120 was chosen
specifically to keep the one-time formatting-adoption diff mechanical
(whitespace/line-break only) instead of forcing a much larger reflow of
existing, unrelated code. That one-time `ruff format` pass is included in
this PR as an isolated, tool-generated commit -- it changes no logic, and the
full pytest suite passes unchanged before and after it.

**The lint rule selection is deliberately narrow:** `E4`, `E7`, `E9` (Ruff's
own pycodestyle defaults -- import position, statement-level issues, syntax
errors), `F` (pyflakes correctness: unused imports/variables, undefined
names, ...), and `I` (import sorting). Rule families like `B`
(flake8-bugbear), `SIM` (flake8-simplify), `UP` (pyupgrade) or `RUF`
(Ruff-specific) are **not** enabled yet. Those flag real code patterns worth
a human decision on a case-by-case basis, not just formatting -- turning them
on wholesale on adoption is exactly the "mass unrelated rewrite" this gate is
meant to avoid triggering. Enabling one of those families is a deliberate,
reviewed follow-up, not a side effect of keeping this gate green.

## Incremental type checking (mypy)

**Tool:** [mypy](https://mypy-lang.org/), configured in `bbbffl_app/pyproject.toml`'s
`[tool.mypy]` section.

**Reproduce locally:**

```bash
cd bbbffl_app
mypy
```

**Current scope** -- exactly the files below (also `[tool.mypy]`'s `files`
list; `tests/test_type_checking_scope.py` keeps the two in sync so scope
drift can't happen without a matching documentation update):

<!-- mypy-scope:start -->
```
app/audit.py
app/season.py
app/identity.py
app/round_mapping.py
app/fixtures.py
```
<!-- mypy-scope:end -->

**Why these five:** they are the season-domain persistence boundary
described in [`architecture.md`](architecture.md) -- the append-only audit
log, and the season/competition, coach/season-entry identity, and
round-mapping repositories -- rather than the routed HTTP surface or the
larger, still-evolving lineup/lockout/ownership modules. They are
self-contained (`app/audit.py` has zero external BBBFFL dependencies; the
others depend only on `app.audit` and `app.db`), which kept the annotation
work required to pass the gate honest and mechanical (adding real parameter/
return types) rather than needing broad `Any` or `# type: ignore` to force a
pass. That is itself the guardrail against the failure mode this gate exists
to avoid: a green type-check that was made green by turning typing off
everywhere it got hard, rather than by describing the code accurately.

**Configuration notes:**

- `disallow_untyped_defs`, `disallow_incomplete_defs`, `check_untyped_defs`,
  `no_implicit_optional`, `warn_redundant_casts`, `warn_unused_ignores`, and
  `strict_equality` are all on for the in-scope files -- this is a real,
  meaningful check on those five files, not a token gate.
- `follow_imports = "silent"`: mypy still uses the *real* inferred
  signatures of modules the in-scope files import (e.g. `app.db`), so a call
  into an out-of-scope module is still checked against its actual behaviour,
  but errors are not reported *for* that out-of-scope module. Without this,
  adding one file to `files` would transitively pull in every module it
  imports and silently expand the required gate far past what was reviewed
  and documented here.
- `ignore_missing_imports = true`: third-party stub gaps (e.g. SQLAlchemy
  Core's dynamically-typed row objects) are not this gate's concern.

**Expanding the scope:** add the file's path to both `[tool.mypy].files` in
`pyproject.toml` and the fenced list above, then run `mypy` and fix what it
reports with real annotations -- not `Any`/`# type: ignore` used to force a
pass. Prefer expanding one cohesive module (or a small, related group) at a
time over a single sweep across the whole `app/` package; the goal is each
addition stays reviewable on its own, not that the whole codebase gets typed
in one PR. There is no fixed target date for full-repository coverage --
issue #39 and roadmap package 07 explicitly scope this to an incremental
start, not a repository-wide typing rewrite.

## Migration integrity

This gate was already substantially built by roadmap package 01 / issue #16
(versioned Alembic migrations) -- issue #39's job here is to keep it required
and clearly attributable, not to duplicate it. See
[`database-migrations.md`](database-migrations.md) for the full migration
architecture, and its own stated CI policy:

> CI must cover fresh install, realistic legacy upgrade, idempotence,
> supported downgrade, refusal boundaries, and repository semantics on
> SQLite. It must also run the history and a representative repository write
> against PostgreSQL.

**Reproduce locally:**

```bash
cd bbbffl_app
pytest tests/test_db_migration.py    # fresh/upgrade/idempotence/downgrade/refusal boundaries, SQLite (hermetic)
```

The PostgreSQL half (`postgres-migrations` in `ci.yml`) upgrades a disposable
`postgres:16` service container from a mid-history revision through head
twice (proving idempotence), exercises the append-only audit trigger and the
frozen-fixture-draw immutability triggers with real `IntegrityError`/
`DBAPIError` assertions, and proves a non-default season-length downgrade is
correctly refused rather than silently discarding configuration -- all
against a real PostgreSQL server the job itself provisions, never a
developer's local database. It finishes by running the dedicated PostgreSQL
concurrency suites
(`test_competition_lifecycle_concurrency.py`, `test_lineups_concurrency.py`,
`test_lockouts_concurrency.py`, `test_lineup_correction_concurrency.py`),
which the ordinary `test` job cannot run because they require
`BBBFFL_DATABASE_URL` to point at real PostgreSQL to exercise
`SELECT ... FOR UPDATE`/`ON CONFLICT` semantics SQLite does not have. To run
the same suite locally against your own PostgreSQL instance:

```bash
cd bbbffl_app
export BBBFFL_DATABASE_URL=postgresql+psycopg://bbbffl:bbbffl@localhost:5432/bbbffl_test
python -m app.migrations upgrade
pytest -q tests/test_competition_lifecycle_concurrency.py tests/test_lineups_concurrency.py tests/test_lockouts_concurrency.py tests/test_lineup_correction_concurrency.py
```

## Dependency/security policy

**Tool:** [pip-audit](https://pypi.org/project/pip-audit/) (the PyPA project;
free, no account/API key required), invoked through
`bbbffl_app/scripts/dependency_audit.py` rather than called directly, so the
suppression policy below is enforced identically in CI and locally.

**Reproduce locally:**

```bash
cd bbbffl_app
python -m scripts.dependency_audit
```

**Pass/fail policy:** every known vulnerability pip-audit reports against a
runtime dependency (`requirements.txt`) fails the gate, with no severity
threshold below which a finding is silently allowed through. This is a
deliberate choice, not an oversight: pip-audit's advisory sources (the PyPI
Advisory Database / OSV) do not consistently carry a normalized severity
score, so a threshold would create a false sense of precision rather than
real risk-based filtering. `--strict` additionally fails the run if
pip-audit cannot fetch advisory data for a dependency at all, so a transient
lookup failure reads as red, never as a silent pass.

**Exception policy:** the only way to make a specific, already-triaged
finding non-blocking is a time-boxed entry in
`bbbffl_app/security/pip-audit-ignore.toml`, each naming:

- `id` -- the advisory identifier pip-audit reports;
- `reason` -- why it isn't actionable right now and what would resolve it;
- `owner` -- who is responsible for re-reviewing it;
- `review_by` -- an ISO date after which the entry stops suppressing the
  finding automatically.

`scripts/dependency_audit.py` treats a malformed entry, or one whose
`review_by` has passed, as a **policy violation that fails the gate** --
never as "suppress anyway" or "silently stop suppressing." This is what
stops an advisory-database change from turning an otherwise-valid build
permanently red with no path forward (a documented, owned exception exists),
while also stopping that exception from quietly becoming permanent (it
expires and must be re-reviewed). `tests/test_dependency_audit_policy.py`
proves this parsing/expiry behaviour directly, and proves the real committed
`pip-audit-ignore.toml` currently parses and is unexpired.

**Current exceptions:** seven advisories against `starlette` (a transitive
dependency pulled in by `fastapi==0.115.0`, which pins
`starlette<0.39.0,>=0.37.2`). No starlette release inside that range carries
the fix for any of the seven; the resolving change is a FastAPI
major-version upgrade, which is a materially larger, separate piece of work
than this issue's CI-tooling scope (see
[Deliberate limitations / follow-up](#deliberate-limitations--follow-up)).
The one fixable finding this gate surfaced on adoption -- three `jinja2`
advisories, patched in 3.1.5/3.1.6 with no compatibility constraint from any
other dependency -- was fixed directly by bumping `jinja2` to `3.1.6` in this
PR, rather than suppressed.

## Hermetic vs. credentialed diagnostics

Every job in `ci.yml` (see the table above) is hermetic: it runs from a clean
checkout, provisions any service it needs (PostgreSQL) itself, and never
talks to a live `afl-api` deployment or uses production credentials. All six
are required status checks.

Live/credentialed diagnostics are a completely separate, `workflow_dispatch`
(manual-trigger-only) workflow: `.github/workflows/integration-diagnostics.yml`,
which runs `scripts/afl_contract_diagnostic.py` (issue #18) against a real
configured `afl-api` deployment. Because it only ever runs on
`workflow_dispatch`, it **cannot** run on a pull request or push, and
therefore cannot become an accidentally-required merge gate -- promoting it
would require a deliberate, separate change (adding it to `ci.yml`'s
triggers and to branch protection), not just leaving it enabled. See
[`afl-api-v1-contract.md`](afl-api-v1-contract.md) for what the diagnostic
validates and how to run it with real credentials.

## Python test runtime and slow CI runs

Issue #218. The `test` job runs the **full** regression suite on every PR
and push -- no sharding, parallelism, test selection or runtime cap. It
takes ~21-30 min normally. On a small number of runs the hosted runner is
several times slower. This section explains how to tell that apart from a
real regression or a hung test.

### What the job reports

The `Run tests` step enables the opt-in plugin
`bbbffl_app/tests/ci_observability.py` plus two pytest built-ins. None of
them changes which tests run, their order, fixtures, isolation or outcomes
(`tests/test_ci_observability.py` pins that):

| Signal | Where | Meaning |
|---|---|---|
| `[ci-progress] environment at start/end: ... fsync_p50=...ms io_pressure_avg60=...%` | top and bottom of the step log | Runner size and disk health. A normal hosted runner shows fsync around ~1 ms or less. |
| `[ci-progress] +25m00s done 1180/2081 (+230 in last 5m00s) proc_cpu=..% iowait=..% steal=..% fsync_p50=..ms current=<test> [setup 0m03s]` | every 5 min | Progress, throughput, whether the process is computing or waiting, and what is running right now. |
| `[ci-progress] SLOW: <test> has been in <setup/call/teardown> for 2m00s` | once per test phase past 2 min | A single test/fixture is unusually slow. Only a report: the test is not failed or interrupted. |
| `Timeout (0:10:00)!` followed by thread stacks | if one test runs > 10 min | `faulthandler_timeout=600`: shows where every thread is stuck. The test keeps running. |
| `slowest 25 durations` / `slowest 15 test files` | end of the step log | Slowest setup/call/teardown phases and the files that took the most time. |
| **Summarise test timings** job summary | run summary page | Same tables. Written even if tests failed or the run was **cancelled**. A cancelled or early-stopped run is marked **INCOMPLETE** and names the last test that finished and the test (and phase) running when it was stopped. |

To check whether a slow run was slow everywhere or only in particular
tests, compare two timing logs locally:

```bash
cd bbbffl_app
python -m pytest -p tests.ci_observability --ci-timing-log /tmp/now.jsonl   # any pytest args
python -m scripts.ci_test_timing_report /tmp/now.jsonl --baseline /tmp/normal.jsonl
```

A **uniform slowdown** (at least three quarters of the files materially
slower) points at the environment. **N file(s) slowed down at least 3x as
much as the median** points at those files. **Mixed** means some files are
slower and others aren't: read the table. With fewer than 5 comparable
files the report doesn't classify the result at all.

### Baseline and what the #218 investigation found

- **Normal:** the last 60 `CI` runs before #218's fix took a median of
  25.8 min end to end (p10 22.4, p90 30.4). A normal `Run tests` step is
  ~21-27 min for ~2,080 tests. PR #217's successful rerun on `219bed2` took
  23 min 29 s (`2017 passed, 64 skipped`).
- **Where the time goes:** almost all of it is SQLite-backed tests. Most of
  those build a fresh database and run all Alembic migrations, once in a
  fixture and again in app startup for HTTP tests, then make many small
  committed writes. A profiled local full run on `main` (`c750c60`: 2113
  passed, 64 skipped) ran **1,594 Alembic upgrades totalling ~27 min of its
  ~38 min (~72%)**. Pure-Python tests (AFL client/contract, config,
  architecture, ...) take ~1-2 s in total. The slowest single test takes
  ~9-13 s, so no individual test dominates.
- **What a healthy run looks like with the new output** (this PR's first CI
  run, `c992810`): `Run tests` took 24m35s, with 4 heartbeats of ~390-610
  tests per 5 min, `fsync_p50` 0.25-0.75 ms, `iowait` ~4%, `steal` 0%.
  Per-file times matched the #217 rerun (x0.98 median, x0.90-1.09).
- **The #217 "stuck" run was not stuck.** Its pytest output, still in the
  job log, shows files finishing steadily until it was cancelled at 77 min
  and 55% of the suite. Every DB-backed file was **~6.3x slower** (median;
  range 2.9-8.4x) than in the rerun on the identical commit. The slowdown
  held steady across the whole run (per-block ratios 6.0-6.8x from the first
  file to the last). The 13 files that do no database work were **not**
  slower: 0.9 s in the slow run vs 1.4 s in the rerun. At that pace it
  would have finished after about 2.5 h.
- **Another outlier** (`c747c16`, run 35428685824, 61 min, passed) shows the
  same pattern at ~2.4x (median; per-block 1.8-3.7x throughout the run).
  Again the non-database files were not slower: 1.3 s vs 2.0 s.
- No single test, fixture or file stood out, and neither run failed. The
  slowdown started with the first file, so it did not grow with test order
  or accumulated state. It hit I/O-heavy tests and left CPU-only tests
  alone.

**Best-supported explanation:** occasional GitHub-hosted runners with much
slower disk writes (fsync-bound SQLite work). The cause is not the tests or
the application, not test order or leaked state, and not dependency
installation, which took ~20 s in every run examined. This can't be proven
from the historical logs alone, because they contain no disk metrics. The
new `fsync_p50` / `iowait` / `io_pressure` / `steal` fields exist to confirm
or rule it out the next time it happens.

### What to do with a slow run

1. **Leave it running** if the heartbeat's `done` count keeps rising. This
   is true even at a fraction of the normal rate. Then check the
   environment line: a high `fsync_p50` (tens of ms), `iowait` or
   `io_pressure` with no SLOW lines means the runner is slow and the suite
   is fine. It will finish, just late. You may still cancel and rerun
   (step 4) to get a faster runner. That is a time trade-off, not a fix.
2. **Look into a test** if a `SLOW:` line or `slowest durations` entry names
   the same test or fixture across runs, or the timing-report comparison
   flags specific files rather than a uniform slowdown. Treat that as
   application/test work: the named phase (`setup` = fixtures, `call` = the
   test body) says where to look.
3. **Treat it as stalled** if two or more heartbeats in a row say
   `NO TESTS FINISHED SINCE LAST HEARTBEAT`, on the same `current=` test,
   and especially once `faulthandler` has dumped stacks for that test. Also
   treat it as stalled if the step log stops advancing with no heartbeat for
   well over 5 min, which means the process itself is wedged. A stall with
   high `proc_cpu` usually means a loop in the code. One with ~0% CPU means
   waiting on I/O, a lock or a subprocess. Keep the faulthandler stacks:
   they are the evidence.
4. **Cancel and rerun safely.** Every job is hermetic, and a rerun starts
   on a fresh runner from a clean checkout. Cancelling can't leave state
   behind, and "Re-run failed jobs" / "Re-run all jobs" on the same commit
   is safe. Before cancelling, note what you've seen (heartbeat lines, SLOW
   lines, the **INCOMPLETE** summary's last test) in the PR. A rerun that
   passes after a stall is **not** proof of an infrastructure fault if the
   stall named a test. Only a uniform slowdown or an environment-only stall
   justifies calling it runner variability.

There is deliberately no `timeout-minutes` below GitHub's default (6 h)
on this job. Slow-but-progressing runs are legitimate and must not become
flaky failures, so the signals above are for humans to act on.

## Deliberate limitations / follow-up

- **Type-check scope** covers five domain/audit modules, not the routed HTTP
  surface, the AFL client/resilience layer, or the larger lineup/lockout/
  ownership modules. Expanding it is explicitly incremental (see above); this
  PR does not attempt a repository-wide typing pass.
- **Lint rule set** covers correctness/import-hygiene rules only (see above).
  Adopting a stylistic/refactor rule family (`B`, `SIM`, `UP`, `RUF`, ...) is
  a deliberate future PR, reviewed on its own, not bundled here.
- **Starlette advisories**: seven pip-audit findings are suppressed pending a
  FastAPI major-version upgrade (see above), reviewed by 2026-11-30.
  Upgrading FastAPI/Starlette is recommended as separate follow-up work, not
  part of this issue.
- **Per-test database setup cost (#218 follow-up).** Most SQLite-backed tests
  build a fresh database by running every Alembic migration. That happens in
  `tests/db_helpers.migrated_connection`, in direct `migrate()` fixtures, and
  again in app startup for HTTP tests, and it is the largest fixed cost in
  the suite (see `--durations`: the slowest phases are `setup`). A possible
  optimisation is to migrate one template file per session and copy it per
  test, keeping migration tests on the real path. That would change how
  most of the suite gets its database, so it needs its own reviewed change
  proving schema equivalence. It is also the largest available saving: about
  72% of local suite time is spent in migrations, and it would also shrink
  the fsync-heavy work that makes slow runners slow. It was deliberately not bundled into #218,
  which is about observability.
- **Migration integrity** reuses the package 01/#16 test infrastructure as-is;
  this issue did not add new migration tests, since the existing SQLite
  fresh/upgrade/downgrade/refusal suite plus the PostgreSQL
  upgrade/trigger/concurrency job already satisfy the documented policy in
  `database-migrations.md`.
