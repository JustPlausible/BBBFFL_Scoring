# Production deployment, readiness, backup and rollback baseline

**Issue:** [#243 -- Establish production deployment, readiness, backup and
rollback baseline for v0.1](https://github.com/JustPlausible/BBBFFL_Scoring/issues/243).
**Readiness source:** `docs/2027-live-season-readiness.md`, remaining item
10, and the original roadmap package 39
(`docs/roadmap/2027-season-roadmap.md`).
**Scope:** the self-hosted, ten-coach production deployment BBBFFL actually
needs, not enterprise-scale infrastructure. Prefer this document and the
checked-in configuration under `compose.production.yaml`/
`deploy/production/` over improvised host state -- if a procedure changes,
update the files first and this document second.

This document is the operator runbook. `docs/2027-live-season-readiness.md`
records what issue #243 leaves as still-outstanding/environment-specific;
this document does not restate that classification.

## Contents

- [Production topology](#production-topology)
- [Reproducing on a clean host](#reproducing-on-a-clean-host)
- [Secrets and configuration](#secrets-and-configuration)
- [HTTPS / reverse proxy](#https--reverse-proxy)
- [Readiness vs liveness](#readiness-vs-liveness)
- [Scheduled backups](#scheduled-backups)
- [RPO and RTO](#rpo-and-rto)
- [Restore procedure](#restore-procedure)
- [Logging and alerting](#logging-and-alerting)
- [Release procedure](#release-procedure)
- [Rollback strategy](#rollback-strategy)
- [What remains environment-specific](#what-remains-environment-specific)

## Production topology

`compose.production.yaml` (repository root) defines four services:

| Service | Image | Reachable from | Persistent state |
|---|---|---|---|
| `app` | built from `bbbffl_app/` (this repo) | `proxy` only, via the compose project's default network | `production-app-data` volume (`/app/data` -- the legacy teams-config mount point; the season model's authoritative state is entirely in `database`) |
| `database` | `postgres:16-alpine` | `app`, `backup` only -- **never published to the host or the internet** | `production-database` volume (PostgreSQL's data directory) |
| `backup` | `postgres:16-alpine` (same image as `database`, so `pg_dump`'s version always matches the server) | outbound only (to `database`, and optionally an alert webhook) | `deploy/production/backups/` (host bind mount, outside every container's writable layer) |
| `proxy` | `caddy:2-alpine` | the public internet on 80/443 -- the **only** service this deployment exposes | `production-proxy-data`/`production-proxy-config` volumes (Let's Encrypt account key + issued certificates) |

Dependency/restart behaviour: `app` waits for `database`'s healthcheck
(`pg_isready`); `backup` waits for the same; `proxy` waits for `app`'s own
`HEALTHCHECK` (liveness, from `bbbffl_app/Dockerfile`). Every service has
`restart: unless-stopped`, so a container that crashes (or the host
rebooting) recovers without operator action, and an operator-initiated
`stop` stays stopped.

## Reproducing on a clean host

Prerequisites: Docker with the Compose plugin, this repository checked out,
and (for real production use, not rehearsal) DNS for the chosen domain
already pointing at the host and ports 80/443 reachable from the internet.

```bash
cp bbbffl_app/.env.production.example bbbffl_app/.env.production
# edit bbbffl_app/.env.production -- fill in every CHANGE-ME value (see
# "Secrets and configuration" below)
chmod 600 bbbffl_app/.env.production

cp deploy/production/Caddyfile.example deploy/production/Caddyfile
# edit if the rehearsal/no-DNS alternative in that file is needed

mkdir -p deploy/production/backups

export BBBFFL_RELEASE_TAG=$(git rev-parse --short HEAD)
docker compose -f compose.production.yaml build app
docker compose -f compose.production.yaml up -d
```

Validate before treating a release as successfully deployed:

```bash
docker compose -f compose.production.yaml config --quiet   # fails on any invalid/missing env_file, port conflict, etc.
docker compose -f compose.production.yaml ps                # every service "healthy" or "running"
curl -f https://<BBBFFL_DOMAIN>/health                       # liveness
curl -f https://<BBBFFL_DOMAIN>/health/ready                  # dependency readiness -- see below
```

`app/config.py`'s `get_settings()` fails closed at startup (before
migrations run) if a required production value is missing or invalid, so a
misconfigured `.env.production` is reported in `docker compose logs app`
rather than starting in a half-working state.

This exact sequence -- build, `up -d`, then the validation block above --
was exercised during this issue's work in a disposable rehearsal
environment (a real `postgres:16-alpine` database, a real built application
image, a real Caddy reverse proxy). See
[`evidence/production-operations-rehearsal-2026-09-27.md`](evidence/production-operations-rehearsal-2026-09-27.md)
for the exact commands and observed output.

## Secrets and configuration

Every secret/URL a production deployment needs lives in
`bbbffl_app/.env.production` (copied from
`bbbffl_app/.env.production.example`, which is the only version checked
into the repository):

| Variable | Purpose | Handling |
|---|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL role password | Generate a long random value (`openssl rand -hex 32`). Never reused across environments. |
| `BBBFFL_DATABASE_URL` | App's PostgreSQL connection string | Must embed the same password as `POSTGRES_PASSWORD` above -- see the note in `.env.production.example` on why these are not derived from each other automatically. |
| `BBBFFL_ADMIN_TOKEN` | Shared legacy admin-interface token | `openssl rand -hex 32`. Required in production -- `get_settings()` refuses to start without it (see `docs/settings.md`). |
| `BBBFFL_SESSION_SECRET` | Coach sign-in/sign-out CSRF signing key | `openssl rand -hex 32`. The development placeholder is refused outright in production. |
| `AFL_API_KEY` | afl-api credential, if the deployment requires one | Supplied by the afl-api operator; never logged (see `app/config.py`'s module docstring). |
| `BBBFFL_ALERT_WEBHOOK_URL` | Optional alert delivery | A Slack incoming webhook, ntfy.sh topic, or similar. Not a secret in the same sense as the above, but still kept out of the repository since it is deployment-specific. |

**What is safe to commit:** `bbbffl_app/.env.production.example` and
`deploy/production/Caddyfile.example` only -- both contain placeholder
`CHANGE-ME` values, never a real credential. The filled-in
`bbbffl_app/.env.production` and `deploy/production/Caddyfile` are excluded
by `.gitignore` (bbbffl_app's and the repo root's respectively).

**How secrets reach the containers:** every service's `env_file:` entry
points at the one `bbbffl_app/.env.production` file -- Docker Compose reads
it and injects the values as that container's environment; it is never
baked into the built image. Storage expectations for that file (and for
`deploy/production/Caddyfile`, which holds no secret but does hold the
production domain):

- owned by the operator account that runs `docker compose`, not world- or
  group-readable (`chmod 600`);
- lives only on the production host's filesystem (or a password
  manager/secrets vault the operator already uses for other credentials --
  this issue does not introduce a new secrets-management product, per
  `docs/settings.md`'s existing "non-goals preserved" note);
- never emailed, pasted into a chat, or committed, including in a private
  fork -- copy the `.example` file and edit the copy in place on the host.

**Certificate/private-key handling:** Caddy generates and renews the TLS
certificate itself (Let's Encrypt, HTTP-01 challenge) and stores the
account key and issued certificate in the `production-proxy-data` Docker
volume -- there is no certificate/key file for the operator to generate,
store, or rotate by hand. Back up that volume (`docker run --rm -v
production-proxy-data:/data -v $PWD:/backup alpine tar czf
/backup/caddy-data.tgz /data`) if avoiding a Let's Encrypt re-issuance after
a host rebuild matters; losing it only costs one re-issuance, never data
loss, since Let's Encrypt has no meaningful rate limit for a single
low-traffic domain re-requesting a certificate.

**Database credentials specifically:** never appear in a log line (see
`app/config.py`'s module docstring: "never logging a secret's value") and
never appear in `GET /health/ready`'s response (see
["Readiness vs liveness"](#readiness-vs-liveness) below -- a database
failure reports only the exception's type name, never its message). The
`backup` service's cron job sources them from a root-only (`chmod 600`),
never-volume-persisted file regenerated on every container start -- see
`deploy/production/scripts/backup_entrypoint.sh`'s own comment for why this
exists (Alpine's `crond` does not inherit the container's environment).

`app/config.py`'s existing fail-closed production validation is unchanged
by this issue -- every check documented in `docs/settings.md` still applies
verbatim; this issue only adds `BBBFFL_READINESS_TIMEOUT_SECONDS` (optional,
defaults to `5`, see below) to that boundary.

## HTTPS / reverse proxy

`deploy/production/Caddyfile.example` is the checked-in template; see its
own comments for exactly what it forwards, which headers it preserves
(Caddy's `reverse_proxy` sets `X-Forwarded-For`/`-Proto`/`-Host` and
preserves the original `Host` header by default -- nothing here strips or
rewrites `Authorization`/`X-Admin-Token`), and its explicit HTTPS approach
(automatic Let's Encrypt provisioning/renewal, HTTP redirected to HTTPS).
PostgreSQL is never reverse-proxied, and `compose.production.yaml` never
publishes the `database` service's port at all -- the proxy has no route to
it, and neither does anything else outside the compose project's internal
network.

What remains environment-specific (cannot be provided generically in a
checked-in file): `BBBFFL_DOMAIN` must be a real hostname that already
resolves to the production host before first start, so Let's Encrypt's
HTTP-01 challenge can succeed, and ports 80/443 must be reachable from the
internet (router port-forwarding for a home-server deployment). The
Caddyfile's own comments document a `tls internal` rehearsal alternative
for a host with no public DNS -- this issue's own rehearsal used exactly
that alternative (see the evidence document linked above).

## Readiness vs liveness

`GET /health` (`bbbffl_app/app/routes/health.py`) is unchanged by this
issue: `{"status": "ok"}`, no database or afl-api dependency at all. It
stays what the Dockerfile's own `HEALTHCHECK` and Compose's
`condition: service_healthy` use for restart/liveness decisions -- a
transient afl-api or database outage must never look like a crashed
process and trigger a restart-loop that would not fix the outage.

`GET /health/ready` is new. It checks:

- **database connectivity** -- a bare `SELECT 1`, never a write;
- **afl-api connectivity**, only when `settings.afl_mode == "live"` -- the
  same unauthenticated `GET /api/{version}` discovery endpoint
  `scripts/afl_contract_diagnostic.py` already uses as its own connectivity
  smoke check (`AflApiClient.check_connectivity`), never an endpoint that
  pulls a real season/round/player dataset or performs fantasy-domain
  work. Replay/test runs (`afl_mode == "replay"`) report this check
  `"skipped"` and never make a live network call -- see the module's own
  docstring for why this keeps the hermetic test suite deterministic.

Both checks are bounded by `BBBFFL_READINESS_TIMEOUT_SECONDS` (default `5`
seconds each), run off the event loop via `asyncio.to_thread` +
`asyncio.wait_for`, so a stuck dependency can never hang the response
indefinitely -- a timeout is reported the same way any other failure is.
Response shape:

```json
{"status": "ok", "checks": {"database": {"status": "ok"}, "afl_api": {"status": "ok"}}}
```

or, on failure, HTTP 503 with the failing check(s) marked `"error"` and a
short `"detail"`. A database failure's `detail` is only the exception's
*type name* (e.g. `"OperationalError"`) -- a driver error message is not
guaranteed secret-safe. An afl-api failure's `detail` is the full message,
which is secret-safe by construction (`app/afl_client.py`'s error classes
never include request headers, so never the API key -- see their own
docstrings). Neither check ever mutates state. See
`bbbffl_app/tests/test_health_api.py` for the automated success/failure/
timeout/replay-skip coverage, and the evidence document linked above for
this check exercised against a real PostgreSQL and a real (deliberately
unreachable) afl-api endpoint.

This readiness endpoint is deliberately not wired into the Dockerfile's own
`HEALTHCHECK` or into `proxy`'s dependency condition -- see
["Logging and alerting"](#logging-and-alerting) for the separate, external
monitoring path that uses it instead, and why.

## Scheduled backups

The `backup` service (`compose.production.yaml`) installs a crontab entry
from `BBBFFL_BACKUP_SCHEDULE` (default `15 2 * * *` -- daily at 02:15) and
runs Alpine's `crond` in the foreground as its PID 1
(`deploy/production/scripts/backup_entrypoint.sh`). Each run
(`deploy/production/scripts/backup_postgres.sh`):

- runs `pg_dump -Fc` (PostgreSQL's custom compressed format -- portable
  across minor versions and what `pg_restore` expects) against the
  `database` service;
- names the output `bbbffl-<database>-<UTC timestamp>.dump` (e.g.
  `bbbffl-bbbffl-20270101T021500Z.dump`), so origin database and exact
  capture time are unambiguous from the filename alone;
- writes to a temporary `.in-progress` name first and atomically renames
  it on success, so a reader (or a concurrent retention sweep) never sees
  a partially written file;
- prunes files older than `BBBFFL_BACKUP_RETENTION_DAYS` (default `14`)
  after a successful run only -- a failed run never deletes anything;
- on failure, logs `CRITICAL` and, if `BBBFFL_ALERT_WEBHOOK_URL` is
  configured, POSTs a short alert, then exits non-zero (visible via
  `docker compose -f compose.production.yaml logs backup` and via the
  container's own exit-status history in `docker compose ps -a`).

Files land in `deploy/production/backups/` -- a host bind mount, outside
every container's writable/ephemeral layer, so removing or recreating the
`backup` (or `database`) container never touches existing backups. Choosing
02:15 UTC as the default schedule: BBBFFL rounds are scored and published
on a weekly cadence with no defined activity at that hour for any
Australian timezone the league operates in, so a backup never runs
concurrently with an actual scoring/lineup-submission window. 14 days'
retention covers more than one full ordinary round's worth of history
(rounds are weekly) while keeping disk usage bounded on a home-server host;
see ["RPO and RTO"](#rpo-and-rto) for how this schedule and retention were
chosen together with the accepted recovery targets.

`deploy/production/scripts/restore_postgres.sh` is the paired restore tool
-- see ["Restore procedure"](#restore-procedure) below.

## RPO and RTO

**Proposed, not yet product-owner-accepted** -- Steve should confirm or
adjust these before v0.1 is considered operationally deployed (see
`docs/2027-live-season-readiness.md`).

| Target | Proposed value | What it means operationally |
|---|---|---|
| **RPO** (Recovery Point Objective) | **24 hours** | With daily backups at 02:15 UTC, the worst case is losing up to a day's lineup submissions/scoring decisions if the database is lost immediately before the next scheduled backup. For a competition that runs on a weekly (not daily) cadence -- one round's lineups/scores per week -- a day's worth of writes is, at most, one evening's lineup edits by however many coaches touched their team that day, never a full round's results (which are only computed after matches conclude, well inside a 24h window of the next backup). |
| **RTO** (Recovery Time Objective) | **4 hours** | Time from "the production database is confirmed lost/corrupted" to "a coach can submit a lineup again," assuming the operator is available: `docker compose down`, restore the latest backup into the `database` volume (or a freshly created one), `docker compose up -d`, verify readiness -- see ["Restore procedure"](#restore-procedure). The bulk of this budget is operator response time and verification, not the restore itself, which this issue's rehearsal completed in well under a minute against a small database (see the evidence document). |

These are realistic for a ten-team fantasy competition on this topology,
not enterprise-style numbers the implementation cannot support: there is no
continuous replication or point-in-time-recovery WAL archiving here (which
would tighten RPO toward zero at meaningfully more operational complexity
than a small self-hosted league needs), and there is no standby database
to fail over to (which would tighten RTO). If Steve's actual tolerance is
tighter than 24h/4h, the schedule (more frequent backups) or topology
(WAL archiving, a standby) would need to change to match -- this document
does not silently assert acceptance of figures that need his confirmation.

## Restore procedure

`deploy/production/scripts/restore_postgres.sh <dump-file> <target-database>`
restores a `pg_dump` custom-format backup into a named database, always
run inside the `backup` service container (it has the exact
`pg_dump`/`pg_restore`/`psql`/`createdb` build matching `database`):

```bash
docker compose -f compose.production.yaml exec -T backup \
  /scripts/restore_postgres.sh /backups/bbbffl-bbbffl-20270101T021500Z.dump bbbffl_staging_restore
```

The target database name is a required, explicit argument with no
default, so this can never silently restore over the live production
database (`PGDATABASE`) just because that variable happens to be set in
the shell it runs in -- restoring over that name is refused outright
(`exit 3`) unless `BBBFFL_ALLOW_RESTORE_OVER_LIVE_DATABASE=yes` is set,
which exists only for the deliberate database-affecting rollback path (see
["Rollback strategy"](#rollback-strategy) below). Day-to-day restore
rehearsal always targets a differently named clean/staging database and
never needs that flag. If the target database does not already exist, the
script creates it; it then runs `pg_restore --clean --if-exists --no-owner`
so a retried restore into an already-partially-restored database is
idempotent (the same convention the existing 2026 replay playbooks use).

**This was rehearsed, not just written down**, during this issue's work:
backup taken from a real running production-topology database (with
representative application data written through the app's own service
layer, not raw SQL) → restored into a genuinely separate, disposable
PostgreSQL container → verified with more than "exited zero": the restored
database's `alembic_version` matched the source's migration head exactly,
the representative DNP decision and its audit-trail row were both present
and correct, and the full ~94-table schema was intact. Full commands and
output: [`evidence/production-operations-rehearsal-2026-09-27.md`](evidence/production-operations-rehearsal-2026-09-27.md).
No preserved replay database or live database was overwritten or even
touched by this rehearsal.

## Logging and alerting

**Logging.** `bbbffl_app/app/main.py`'s `configure_logging` (unchanged
mechanism, driven by the existing `BBBFFL_LOG_LEVEL`) now runs *before*
`get_settings()` can raise, so an invalid production configuration is
reported as a `CRITICAL bbbffl.startup` log line rather than a bare
traceback. The same applies to a migration/database-connection failure
during startup. A dependency-readiness failure (database or afl-api) logs
`WARNING bbbffl.readiness` naming which check failed, every time
`GET /health/ready` is polled and something is down. A new catch-all
`Exception` handler logs every otherwise-unhandled application error at
`CRITICAL bbbffl.startup` (with the method/path and full traceback) before
returning a generic 500 -- rehearsed directly in this issue's work (see the
evidence document): a deliberately broken release surfaced exactly this
log line. All of this goes to stdout/stderr, which Docker's own log driver
retains -- `docker compose -f compose.production.yaml logs <service>` (add
`--since`/`-f` as needed) is the operator's log inspection tool; no
separate logging platform is introduced.

Backup failures log the same way (`CRITICAL`, from
`deploy/production/scripts/lib_alert.sh`'s `bbbffl_log`), visible via
`docker compose -f compose.production.yaml logs backup`.

**Alerting.** A log nobody tails does not detect anything, so this issue
adds one lightweight, reproducible mechanism used consistently by both the
backup script and the readiness watchdog below: `BBBFFL_ALERT_WEBHOOK_URL`,
an optional generic webhook URL (a Slack incoming webhook, an ntfy.sh topic
URL, a healthchecks.io "fail" URL, or anything else that accepts an HTTP
POST) that a failure POSTs a short JSON payload to. This was chosen over a
dedicated observability stack because a ten-coach league does not need
one, and because the same primitive (`lib_alert.sh`'s `bbbffl_alert`)
covers every failure mode this issue lists:

- **backup failure**: `backup_postgres.sh` alerts directly (rehearsed --
  see the evidence document).
- **critical service/dependency failure**: **not** the Docker
  `HEALTHCHECK`/`condition: service_healthy` (those drive restart/liveness
  decisions only, per ["Readiness vs liveness"](#readiness-vs-liveness))
  but `deploy/production/scripts/readiness_watch.sh`, a small host-run
  script (requires `curl`) that polls `GET /health/ready` over the public
  HTTPS path (through the proxy, exercising the same path real traffic
  uses) and alerts on anything other than HTTP 200. Run it on a schedule
  with host cron or a systemd timer, e.g.:

  ```cron
  */5 * * * * BBBFFL_READY_URL=https://<BBBFFL_DOMAIN>/health/ready BBBFFL_ALERT_WEBHOOK_URL=<...> /path/to/repo/deploy/production/scripts/readiness_watch.sh
  ```

  Deliberately host-run rather than containerized: it keeps working even
  if the `app` container itself is down or restarting.

This is intentionally the entire alerting surface for v0.1 -- it does not
implement the broader coach notification/reminder system (roadmap package
40), which is a different, deferred concern per
`docs/2027-live-season-readiness.md`.

## Release procedure

1. **Pre-deployment checks**: CI green on the commit being deployed (see
   `docs/ci-quality-gates.md`); confirm whether the release includes a new
   Alembic revision (`git diff <current-deployed-commit>..<new-commit> --
   bbbffl_app/migrations/`).
2. **Backup immediately before a schema-affecting release**: if step 1
   found a new migration, take an on-demand backup first (do not wait for
   the next scheduled run):
   ```bash
   docker compose -f compose.production.yaml exec -T backup /scripts/backup_postgres.sh
   ```
   A code-only release (no new migration) does not strictly need this --
   the next scheduled backup still covers it -- but taking one costs
   seconds and removes the judgement call, so do it for every release.
3. **Image/revision identification**: build and tag the new image with an
   identifiable revision, never `latest` alone:
   ```bash
   export BBBFFL_RELEASE_TAG=$(git rev-parse --short HEAD)
   docker compose -f compose.production.yaml build app
   ```
   This keeps the previous image (whatever tag it was built with) on disk,
   untouched, as the rollback target in step 7 below.
4. **Migration sequencing**: migrations run automatically at `app`
   startup (`app/main.py`'s lifespan calls `migrate()` before accepting any
   request -- see `docs/database-migrations.md`); there is no separate
   manual migration step for this single-service deployment. A migration
   that cannot run cleanly fails startup, which the readiness/healthcheck
   steps below will catch before real traffic is affected.
5. **Deploy**: `docker compose -f compose.production.yaml up -d app`.
6. **Readiness verification**: `curl -f https://<domain>/health/ready` --
   must return `200` with every check `"ok"` (an afl-api-only failure here
   may be an afl-api-side outage rather than this release; check
   `docker compose logs app` and afl-api's own status before assuming the
   release is at fault).
7. **Basic smoke verification**: load the public Round Centre
   (`GET /`), and, for a release touching auth/lineup/scorer surfaces,
   sign in as a real (or rehearsal) coach and confirm the affected page
   renders.
8. **What makes a release successful**: steps 5-7 all pass, and
   `docker compose ps` shows `app` `healthy` with no restart loop over the
   next several minutes.
9. **What makes the operator stop and roll back**: `app` fails to become
   healthy, `/health/ready` reports the *database* check failing (afl-api
   alone failing is not necessarily this release's fault -- see step 6),
   a `CRITICAL bbbffl.startup` log line appears that traces to the new
   code, or the smoke-tested surface is visibly broken.

## Rollback strategy

`docs/database-migrations.md` (and this repository's own migration policy
in `CLAUDE.md`) is explicit that a downgrade "exists only where data is
unambiguously representable" and otherwise refuses -- several existing
revisions (`0006_players`, `0016_draft_ops`, `0024_opening_round_multi_player`,
and others) already refuse downgrade once irreversible data exists. **A
rollback runbook that says "run `alembic downgrade` and start the previous
image" is therefore unsafe in general**: if the failed release's migration
has already been exercised (any write happened under the new schema), the
downgrade may refuse -- correctly, to avoid silently destroying data it
cannot represent -- leaving that recipe with no next step. This document
instead splits rollback into two cases, exactly as issue #243 requires:

**1. Application-only rollback (the database remains compatible)** -- the
common case: the failed release changed only application code, not the
schema (no new file under `bbbffl_app/migrations/versions/` in the diff).
Roll back by re-deploying the previous image tag -- no database action at
all:

```bash
export BBBFFL_RELEASE_TAG=<previous-known-good-tag>
docker compose -f compose.production.yaml up -d app
```

**Rehearsed in this issue's work**: a deliberately broken release (an
unhandled exception in a route) was deployed, detected via the Docker
`HEALTHCHECK` going unhealthy and a `CRITICAL bbbffl.startup` log line,
then rolled back by re-running the command above against the previous
image tag -- confirmed healthy again within seconds, no database
involvement, no data loss. Full commands/output:
[`evidence/production-operations-rehearsal-2026-09-27.md`](evidence/production-operations-rehearsal-2026-09-27.md).

**2. Database-affecting rollback (the schema or data changed)** -- the
failed release added a migration, and either the migration itself is
broken (fails to apply, or applies but produces wrong data) or the new
application code wrote data in a shape the previous code does not expect.
`alembic downgrade` is **not** the default recipe here -- check first
whether the specific new revision's downgrade is even safe to attempt
(read its own `downgrade()` and any refusal condition it documents,
per `docs/database-migrations.md`'s convention). When it is not
representable, or when data has already been written that a downgrade
would discard, **restoring the pre-release backup taken in the release
procedure's step 2 is the correct safe recovery path**:

```bash
docker compose -f compose.production.yaml stop app        # stop writes first
# $POSTGRES_DB must expand *inside* the backup container (env_file: only
# injects it there, not into the operator's host shell) -- a bare
# "$POSTGRES_DB" on the host expands to empty and both silently bypasses
# the live-database safety guard below and passes an empty target name.
docker compose -f compose.production.yaml exec -T backup sh -c '
  BBBFFL_ALLOW_RESTORE_OVER_LIVE_DATABASE=yes \
    /scripts/restore_postgres.sh /backups/<pre-release-backup>.dump "$POSTGRES_DB"
'
export BBBFFL_RELEASE_TAG=<previous-known-good-tag>
docker compose -f compose.production.yaml up -d app
curl -f https://<domain>/health/ready
```

This is a data-loss operation for anything written since the pre-release
backup -- exactly why step 2 of the release procedure takes that backup
immediately before a schema-affecting release, keeping the exposure window
small and bounded (see ["RPO and RTO"](#rpo-and-rto)). The
`BBBFFL_ALLOW_RESTORE_OVER_LIVE_DATABASE=yes` requirement exists
specifically so this remains a deliberate operator decision, never an
accidental one.

**Rehearsed in this issue's work**, on disposable staging infrastructure
(never a live or preserved replay database): a pre-release backup was
taken, a bad-release-style data corruption was simulated, and restoring
that backup with the override flag above correctly reverted the
corruption while leaving the migration head untouched -- confirmed by
querying the restored data directly, not just the restore command's exit
code. Full commands/output:
[`evidence/production-operations-rehearsal-2026-09-27.md`](evidence/production-operations-rehearsal-2026-09-27.md).

**A failed release's bounded route back to a known usable state** is
therefore always one of the two procedures above, decided by one question
("did this release touch the schema, or did new code write incompatible
data?") -- never an open-ended investigation with no defined endpoint.

## What remains environment-specific

The following cannot be provided generically in this repository and are
Steve's to perform on the real production host -- see
`docs/2027-live-season-readiness.md` for how this issue's completion is
reflected there:

- provisioning the actual production host and its DNS record for
  `BBBFFL_DOMAIN`;
- filling in `bbbffl_app/.env.production`'s real secrets (this document's
  ["Secrets and configuration"](#secrets-and-configuration) section) and
  confirming the real `AFL_API_BASE_URL`/`AFL_API_KEY`;
- accepting (or adjusting) the proposed RPO/RTO targets above;
- validating the real deployed afl-api contract from a network position
  that can actually reach it -- a separate, already-tracked readiness gap
  this issue does not resolve (see `docs/afl-api-v1-contract.md`'s "Live
  validation status" and `docs/2027-live-season-readiness.md`'s live
  afl-api row);
- deciding on, and configuring, the alert-webhook destination
  (`BBBFFL_ALERT_WEBHOOK_URL`) and the host cron/systemd-timer entry for
  `readiness_watch.sh`;
- an initial real-DNS, real-Let's-Encrypt-certificate deployment rehearsal
  (this issue's rehearsal used the Caddyfile's documented `tls internal`
  alternative, since no real production domain exists yet -- see
  ["HTTPS / reverse proxy"](#https--reverse-proxy)).
