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

## Active development

The application CI runs the Python test suite from `bbbffl_app/` and builds
that directory's Docker image. See the application README for the exact
commands and current prototype scope.

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
