# Application settings boundary

**Roadmap:** implements work package **06** from
[`roadmap/2027-season-roadmap.md`](roadmap/2027-season-roadmap.md) (issue #38).

`bbbffl_app/app/config.py`'s `get_settings()` is the single, validated,
typed settings boundary for BBBFFL's critical runtime configuration.
Application code reads `Settings` (via `app.state.settings`, populated once
at startup) rather than `os.environ`/`os.getenv` directly, so every
environment variable name, default, and production requirement lives in
exactly one place instead of being scattered across call sites.

`app/main.py`'s FastAPI `lifespan` calls `get_settings()` as its very first
statement -- before migrations run, before a database connection opens,
and before the app can accept a request. An invalid configuration raises
`SettingsError` there and the process exits without serving any traffic:
the application never partially starts.

## Environments

`BBBFFL_ENVIRONMENT` selects one of three modes (default: `development`):

| Value | Meaning |
| --- | --- |
| `development` | A single operator's home-server/local prototype. Permissive defaults (open admin interface, local SQLite, localhost afl-api) are unchanged from the original prototype behaviour. |
| `test` | Automated/CI test runs. Same permissive defaults as `development`, kept as a distinct declared value so a test run is identifiable in logs/diagnostics rather than indistinguishable from a real deployment. |
| `production` | A real, reachable deployment. Every development default that would be a security or data-integrity risk is refused rather than silently applied. |

**Design constraint:** a development-safe default is never reused to
satisfy a production requirement. Each production-required setting below
is computed *without* a fallback default when `BBBFFL_ENVIRONMENT=production`
-- a missing value is always reported as `<VARIABLE>: required in
production`, never quietly filled in with `http://localhost:8000` or an
open admin interface.

## Production requirements

Set `BBBFFL_ENVIRONMENT=production` and provide every one of:

| Variable | Requirement |
| --- | --- |
| `BBBFFL_DATABASE_URL` | Required; must be a PostgreSQL URL (`postgresql://...` / `postgresql+psycopg://...`). SQLite is refused in production -- see [`database-migrations.md`](database-migrations.md). |
| `BBBFFL_PUBLIC_BASE_URL` | Required; must be an absolute `http(s)://` URL -- the externally reachable base URL of the deployment. |
| `BBBFFL_ADMIN_TOKEN` | Required; the admin interface refuses to start open to any caller in production. |
| `AFL_API_BASE_URL` | Required; must be an absolute `http(s)://` URL. |
| `BBBFFL_AFL_MODE` | Must be `live` (the default) or left unset -- `replay` is refused outright in production. |

Any other configured URL (`BBBFFL_DATABASE_URL`, `BBBFFL_PUBLIC_BASE_URL`,
`AFL_API_BASE_URL`) is format-validated in every environment, not just
production, so a typo fails fast during local development too.

`get_settings()` collects *every* problem it finds and raises them together
in one `SettingsError`, so a first production start with several missing
variables reports all of them at once rather than one failed restart at a
time. Every error names the offending environment variable and states why
it is invalid or missing -- never a secret's value. Logging follows the
same rule: `app/main.py`'s startup log line reports `environment`,
`afl_mode` and the afl-api base URL/contract version, never
`BBBFFL_ADMIN_TOKEN` or `AFL_API_KEY`.

## AFL access mode: live vs. replay

`BBBFFL_AFL_MODE` is `live` (default) or `replay`:

- `live` -- the normal path. `AflApiClient` talks to the configured
  `AFL_API_BASE_URL`.
- `replay` -- declares a deterministic/replay run (roadmap package 32,
  the 2026 season replay harness) against curated evidence instead of a
  live afl-api deployment. Setting it **requires**
  `BBBFFL_AFL_REPLAY_EVIDENCE_PATH` too: replay/deterministic execution
  must never silently fall back to live afl-api access just because that
  path was left unset. `replay` is refused outright when
  `BBBFFL_ENVIRONMENT=production` -- production always uses live access.

This issue validates and exposes the mode; it does not itself build the
replay harness (`Replay: Not required` in issue #38's own scope, and
roadmap package 32 is separate, later work). Today only `live` is wired
into `app/main.py`'s `AflApiClient` construction. A consumer that actually
swaps in deterministic evidence for `replay` arrives with package 32.

## AFL consumer contract version

`AFL_API_CONTRACT_VERSION` (default `v1`) declares which `afl-api`
consumer contract BBBFFL expects, building on the contract validation done
for issue #18 (see
[`afl-api-v1-contract.md`](afl-api-v1-contract.md)). It is validated
against the set of versions this codebase actually implements
(`app.config.SUPPORTED_AFL_API_CONTRACT_VERSIONS`, currently just `("v1",)`)
and passed to `AflApiClient(contract_version=...)`, which builds every
`/api/{contract_version}/...` request path from it. Requesting an
unimplemented version (e.g. `v2`) fails at settings validation, before any
request is made, rather than BBBFFL silently guessing at an incompatible
path.

## Full variable reference

See [`.env.example`](../bbbffl_app/.env.example) for every supported
variable with inline documentation, and `bbbffl_app/app/config.py`'s module
docstring for the authoritative precedence/validation rules. Existing
variables not covered above (`AFL_API_KEY`, the `AFL_API_*_TIMEOUT_SECONDS`
/ `AFL_API_RETRY_*` resilience settings, `BBBFFL_DB_PATH`,
`BBBFFL_TEAMS_CONFIG_PATH`, `BBBFFL_POLL_INTERVAL_SECONDS`,
`BBBFFL_LOG_LEVEL`, `BBBFFL_SUPERSCORE_CONFIG_PATH`) are unchanged by this
issue -- their existing defaults and precedence rules (e.g.
`BBBFFL_DATABASE_URL` winning over `BBBFFL_DB_PATH`, documented in
[`database-migrations.md`](database-migrations.md)) still apply.

## Scope notes

- **Admin/session secrets:** only `BBBFFL_ADMIN_TOKEN` exists today. There
  is no coach session/authentication mechanism yet (roadmap package 19);
  this settings boundary will gain a required session-signing secret when
  that package lands. "as applicable" in issue #38's scope is why only the
  admin token is enforced now.
- **Non-goals preserved:** this issue does not implement authentication,
  a secrets-management product, or any container/orchestrator redesign --
  it only makes existing and previously-implicit configuration explicit,
  typed, and validated. See issue #38's "Non-goals" section.
