# BBBFFL Scoring

Application source for the **Big Bad Bustling Fantasy Football League
(BBBFFL)**.

## Repository structure

- **`bbbffl_app/`** is the active, core application source and the primary
  implementation target for the 2027 rebuild. Start with
  [`bbbffl_app/README.md`](bbbffl_app/README.md) for local development,
  architecture, configuration, and tests.
- **`docs/plans/`** records the agreed 2027 direction and the evidence that
  informs it.
- **`legacy/gas/`** archives the former Google Apps Script, Google Forms, and
  Google Sheets implementation. It is retained as migration and replay
  evidence, not as the target for new feature development.

New BBBFFL features should normally be implemented in `bbbffl_app/`. The
archive must not be treated as a second active application or ported as part
of unrelated work.

## Supported workflows

- **Persistent server / Round 1 rehearsal (recommended for operators):** use
  the Compose-first [`docs/round1-rehearsal.md`](docs/round1-rehearsal.md)
  runbook. It builds the application, provisions its isolated PostgreSQL and
  evidence volumes, bootstraps through a one-off container, and requires no
  host Python installation.
- **Source development:** use the version-pinned Python 3.11 instructions in
  [`bbbffl_app/README.md`](bbbffl_app/README.md#source-development-workflow).
  This is the workflow for editing code and running tests, not the persistent
  home-server deployment path.

## Active development

The application CI runs, as separate required checks so a failure names one
concern: the Python test suite, formatting/lint (Ruff), an incremental
type-check gate (mypy), migration integrity (SQLite and PostgreSQL), a
dependency/security audit (pip-audit), and the Docker image build. See
[`docs/ci-quality-gates.md`](docs/ci-quality-gates.md) (issue #39 / roadmap
package 07) for what each gate covers, the local commands that reproduce
every one of them, and how live/credentialed `afl-api` diagnostics stay
separate from required CI. See the application README for current prototype
scope.

The legacy `clasp` pull/push helpers moved with the GAS projects to
`legacy/gas/`. They are intentionally retired from the normal repository
workflow and are preserved only for maintainers who explicitly need to
inspect or recover the historical Apps Script projects. See
[`legacy/gas/README.md`](legacy/gas/README.md) before using them.

## Database migrations

PostgreSQL is the supported production database; SQLite is supported for local
development, tests, replay, and existing prototype upgrades. Alembic owns
schema evolution. See [`docs/database-migrations.md`](docs/database-migrations.md).

## afl-api `/api/v1` contract

BBBFFL consumes only the public, versioned `afl-api` `/api/v1` contract, never
its internal database, CFS/Champion Data directly, or the deployment
hostname/credentials as a compatibility identifier. The endpoint/field
inventory, semantic contract (identifiers, match lifecycle, stat finality,
timing, player membership), known upstream gaps, and compatibility/pinning
policy are documented in
[`docs/afl-api-v1-contract.md`](docs/afl-api-v1-contract.md).

A versioned, provenance-rich, offline corpus of `afl-api` v1 evidence for
tests and historical replay (never a live data source) lives in
`bbbffl_app/tests/fixtures/afl_evidence/`, loaded through
`bbbffl_app/tests/afl_evidence.py`. See
[`docs/afl-evidence-fixtures.md`](docs/afl-evidence-fixtures.md).
