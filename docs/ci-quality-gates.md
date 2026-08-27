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
`test_lockouts_concurrency.py`), which the ordinary `test` job cannot run
because they require `BBBFFL_DATABASE_URL` to point at real PostgreSQL to
exercise `SELECT ... FOR UPDATE`/`ON CONFLICT` semantics SQLite does not
have. To run the same suite locally against your own PostgreSQL instance:

```bash
cd bbbffl_app
export BBBFFL_DATABASE_URL=postgresql+psycopg://bbbffl:bbbffl@localhost:5432/bbbffl_test
python -m app.migrations upgrade
pytest -q tests/test_competition_lifecycle_concurrency.py tests/test_lineups_concurrency.py tests/test_lockouts_concurrency.py
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
- **Migration integrity** reuses the package 01/#16 test infrastructure as-is;
  this issue did not add new migration tests, since the existing SQLite
  fresh/upgrade/downgrade/refusal suite plus the PostgreSQL
  upgrade/trigger/concurrency job already satisfy the documented policy in
  `database-migrations.md`.
