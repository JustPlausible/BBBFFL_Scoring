# Production operations rehearsal (issue #243)

**Date:** 27 September 2026.
**Scope:** the production deployment topology, readiness endpoint,
scheduled backups, restore procedure, and both rollback strategies
introduced by issue #243, rehearsed end-to-end in a disposable environment.

**Classification:** a disposable rehearsal in the development/CI sandbox
this issue was implemented in -- **not** a rehearsal against the real
production host, real DNS, or the real afl-api deployment. See
`docs/production-operations.md#what-remains-environment-specific` for what
is still Steve's to perform on the actual host. No preserved replay
database, and no real production database, was used or touched by any
step below -- every database here was created fresh for this rehearsal and
discarded afterward.

## Environment

- Docker Engine 29.3.1 + Compose v5.1.1, run directly (no Kubernetes/cloud
  provider involved -- matching the topology this issue targets).
- `compose.production.yaml`, built and run exactly as
  `docs/production-operations.md#reproducing-on-a-clean-host` documents,
  with `bbbffl_app/.env.production` copied from the checked-in
  `.env.production.example` (placeholder secrets -- this is a rehearsal,
  never a real deployment).
- `AFL_API_BASE_URL` deliberately left as the example's placeholder
  (unreachable) -- this rehearsal proves the readiness check correctly
  reports afl-api as down, not that the real afl-api deployment is
  reachable (that remains the separate, already-tracked outstanding item;
  see `docs/afl-api-v1-contract.md`).
- `deploy/production/Caddyfile`'s `tls internal`/`localhost` rehearsal
  alternative was used for the HTTPS step (documented in
  `Caddyfile.example` itself) -- no real DNS or public port-forwarding
  exists in this sandbox.

## A. Clean-host build and startup

```
$ docker compose -f compose.production.yaml build app
...
Image bbbffl-app:latest Built

$ docker compose -f compose.production.yaml up -d database
$ docker compose -f compose.production.yaml up -d app backup
```

Result: `database` reported `healthy` (via `pg_isready`), then `app` ran
every Alembic migration from empty to head
(`0035_coach_draft_shortlist`) and reported:

```
app-1 | 2026-09-27 02:35:40,630 INFO bbbffl.startup: BBBFFL Scoring application starting up (environment=production, afl_mode=live, afl_api=https://CHANGE-ME-afl-api.example.net, afl_api_contract_version=v1, teams=['team_a', 'team_b'])
app-1 | INFO:     Application startup complete.
app-1 | INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

`docker compose ps` showed `app`/`database` both `healthy`, `backup`
`running` with its own startup line:

```
backup-1 | [backup] ready: schedule='15 2 * * *' retention_days='14' backup_dir=/backups
```

## B. Readiness vs liveness, against a real database and a real (unreachable) afl-api

```
$ curl http://localhost:8000/health          # (run inside the app container)
200 {"status": "ok"}

$ curl http://localhost:8000/health/ready
503 {"status": "error", "checks": {"database": {"status": "ok"},
     "afl_api": {"status": "error",
                 "detail": "afl-api connection failed: GET /api/v1 (ConnectError)"}}}
```

`docker compose logs app` recorded the matching structured log line:

```
app-1 | 2026-09-27 02:46:03,486 ERROR bbbffl.afl_client: afl-api request failed: GET /api/v1 ([Errno -5] No address associated with hostname)
app-1 | 2026-09-27 02:46:03,487 WARNING bbbffl.readiness: readiness check failed: afl_api
```

**Through the real HTTPS reverse proxy** (Caddy, `tls internal`, see
above), from outside the compose network entirely:

```
$ curl -sk https://localhost/health -w '\nHTTP=%{http_code}\n'
{"status":"ok"}
HTTP=200

$ curl -sk https://localhost/health/ready -w '\nHTTP=%{http_code}\n'
{"status":"error","checks":{"database":{"status":"ok"},"afl_api":{"status":"error","detail":"afl-api connection failed: GET /api/v1 (ConnectError)"}}}
HTTP=503
```

`docker compose ps --format '{{.Name}}: {{.Ports}}'` confirmed the intended
topology: `database` and `backup` show only container-internal `5432/tcp`
(no host binding), `app` shows only container-internal `8000/tcp`, and only
`proxy` publishes `0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp` -- PostgreSQL
is not reachable from the host or the internet at all, matching issue
#243's explicit requirement.

## C. Representative application data, written through the app's own service layer

```
$ curl -X POST http://localhost:8000/api/admin/dnp -H 'X-Admin-Token: ...' \
    -d '{"team_key": "team_a", "slot": "Forward1", "dnp": true,
         "reason": "backup rehearsal smoke test (issue #243)"}'
```

(The route's own subsequent read-back returned 502, because it also
queries the unreachable afl-api for live match state -- expected and
unrelated to the write itself, which is a separate call inside the same
route handler and had already committed.) Verified directly against
PostgreSQL:

```
$ psql -c "SELECT * FROM slot_dnp;"
 competition_key | team_key |   slot   | dnp |            updated_at
 grand_final     | team_a   | Forward1 |   1 | 2026-09-27T02:37:05.13...

$ psql -c "SELECT action, entity_type, reason FROM audit_event;"
        action        | entity_type  |                  reason
 scoring.dnp.changed   | scoring.slot | backup rehearsal smoke test (issue #243)
```

## D. Scheduled backup mechanism

Ran the exact command `crond` would run on schedule:

```
$ docker compose exec -T backup sh -c ". /etc/bbbffl-backup.env; PGHOST=database /scripts/backup_postgres.sh"
2026-09-27T02:38:41Z [INFO] starting backup of database 'bbbffl' to /backups/bbbffl-bbbffl-20260927T023841Z.dump
2026-09-27T02:38:41Z [INFO] backup succeeded: /backups/bbbffl-bbbffl-20260927T023841Z.dump (271144 bytes)
```

Confirmed the file landed on the host bind mount, outside any container's
writable layer:

```
$ ls -la deploy/production/backups/
-rw-r--r-- 1 root root 271144 Sep 27 02:38 bbbffl-bbbffl-20260927T023841Z.dump
```

**Failure path**, with a bad host/credential and an (intentionally
unreachable) alert webhook:

```
$ PGHOST=bad-host PGPASSWORD=wrong BBBFFL_ALERT_WEBHOOK_URL=http://127.0.0.1:9/nonexistent /scripts/backup_postgres.sh; echo EXIT=$?
pg_dump: error: could not translate host name "bad-host" to address: Name does not resolve
2026-09-27T02:38:51Z [CRITICAL] PostgreSQL backup of 'bbbffl' FAILED at 20260927T023851Z -- see: docker compose -f compose.production.yaml logs backup
wget: can't connect to remote host (127.0.0.1): Connection refused
2026-09-27T02:38:51Z [WARNING] alert webhook delivery failed (BBBFFL_ALERT_WEBHOOK_URL unreachable)
EXIT=1
```

No partial/temporary file was left behind after the failure (confirmed by
listing the backups directory again). This exercised a real bug found and
fixed during this rehearsal: the first version of
`backup_entrypoint.sh` wrote the captured environment with `env` alone,
which is not safely re-sourceable for a value containing spaces
(`BBBFFL_BACKUP_SCHEDULE="15 2 * * *"`) -- fixed to single-quote/escape
each value before writing it, and re-verified with the successful run
above.

## E. Restore procedure, into a genuinely separate disposable database

A second, standalone `postgres:16-alpine` container (`bbbffl-staging-restore`,
its own Docker volume, joined to the same compose network only so the
`backup` container could reach it by name) stood in for a clean/staging
environment, entirely separate from the rehearsal "production" stack above.

```
$ docker compose exec -T backup /scripts/restore_postgres.sh \
    /backups/bbbffl-bbbffl-20260927T023841Z.dump bbbffl_staging_restore
2026-09-27T02:39:15Z [INFO] restoring .../bbbffl-bbbffl-20260927T023841Z.dump into database 'bbbffl_staging_restore' on host 'bbbffl-staging-restore'
2026-09-27T02:39:15Z [INFO] target database 'bbbffl_staging_restore' does not exist yet -- creating it
2026-09-27T02:39:16Z [INFO] restore into 'bbbffl_staging_restore' completed
```

**Verification beyond "exited zero"**:

```
$ psql -d bbbffl_staging_restore -c "SELECT version_num FROM alembic_version;"
 0035_coach_draft_shortlist          <- identical to the source database's head

$ psql -d bbbffl_staging_restore -c "SELECT team_key, slot, dnp FROM slot_dnp;"
 team_a | Forward1 | 1                <- the representative decision from step C, intact

$ psql -d bbbffl_staging_restore -c "SELECT action, entity_type, reason FROM audit_event;"
 scoring.dnp.changed | scoring.slot | backup rehearsal smoke test (issue #243)   <- audit trail intact

$ psql -d bbbffl_staging_restore -c "\dt" | wc -l
94                                     <- full schema present
```

**Safety guard verified**: attempting to restore over the name matching
the live `PGDATABASE` without the override flag was refused:

```
$ /scripts/restore_postgres.sh /backups/bbbffl-bbbffl-20260927T023841Z.dump bbbffl
2026-09-27T02:40:01Z [ERROR] refusing: target 'bbbffl' matches PGDATABASE (the live database).
2026-09-27T02:40:01Z [ERROR] set BBBFFL_ALLOW_RESTORE_OVER_LIVE_DATABASE=yes only for a deliberate rollback recovery -- see docs/production-operations.md#rollback-strategy
EXIT=3
```

The real production/rehearsal database (`bbbffl`, in the `database`
service) was never restored over or otherwise modified by this section.

## F. Application-only rollback rehearsal

1. Tagged the working image `bbbffl-app:release-1`.
2. Built a deliberately broken image `bbbffl-app:release-2-broken`
   (`FROM bbbffl-app:release-1`, overlaying a `health.py` whose `/health`
   handler raises `RuntimeError` -- no dependency changes, no network
   access needed for this derived build).
3. Deployed it: `BBBFFL_RELEASE_TAG=release-2-broken docker compose ... up -d app`.
   Detected via the Dockerfile's own `HEALTHCHECK`:
   ```
   $ docker inspect --format '{{json .State.Health}}' bbbffl_scoring-app-1
   {"Status":"starting","FailingStreak":1,"Log":[{"ExitCode":1,
     "Output":"...HTTPError: HTTP Error 500: Internal Server Error\n"}]}
   ```
   and via the new catch-all exception handler's structured log line:
   ```
   app-1 | 2026-09-27 02:41:19,346 CRITICAL bbbffl.startup: Unhandled exception on GET /health
   ```
4. Rolled back: `BBBFFL_RELEASE_TAG=release-1 docker compose ... up -d app`.
   ```
   $ docker compose ps app
   bbbffl_scoring-app-1   bbbffl-app:release-1   Up 8 seconds (healthy)
   $ curl http://localhost:8000/health
   200 {"status": "ok"}
   ```

No database action was involved at any point -- exactly the "application-
only, database remains compatible" case.

## G. Database-affecting rollback rehearsal (disposable staging only)

Continuing on the standalone `bbbffl-staging-restore` container from
section E (never the rehearsal "production" database, and never a
preserved replay database):

1. Took a "pre-release" backup of its current state
   (`pre-release-rollback-rehearsal.dump`).
2. Simulated a bad release corrupting data: directly inserted an
   unintended second `slot_dnp` row (`Forward2`, `dnp=1`) -- standing in
   for a buggy release writing bad data.
   ```
   $ psql -d bbbffl_staging_restore -c "SELECT team_key, slot, dnp FROM slot_dnp ORDER BY slot;"
    team_a | Forward1 | 1
    team_a | Forward2 | 1        <- the simulated corruption
   ```
3. Performed the database-affecting rollback -- restored the pre-release
   backup over the same (disposable) database, with the explicit override
   flag the real procedure requires:
   ```
   $ BBBFFL_ALLOW_RESTORE_OVER_LIVE_DATABASE=yes /scripts/restore_postgres.sh \
       pre-release-rollback-rehearsal.dump bbbffl_staging_restore
   2026-09-27T02:42:25Z [INFO] restoring ... into database 'bbbffl_staging_restore' ...
   2026-09-27T02:42:26Z [INFO] restore into 'bbbffl_staging_restore' completed
   ```
4. Verified the corruption was gone and the schema/migration head intact:
   ```
   $ psql -d bbbffl_staging_restore -c "SELECT team_key, slot, dnp FROM slot_dnp ORDER BY slot;"
    team_a | Forward1 | 1                     <- back to the pre-release state exactly
   $ psql -d bbbffl_staging_restore -c "SELECT version_num FROM alembic_version;"
    0035_coach_draft_shortlist                <- unchanged
   ```

## H. Post-review fixes, re-verified

Codex review on the PR found six issues across two passes, all fixed and
re-verified against real containers before being marked resolved (none
required re-running the full rehearsal above -- each was independently
reproducible in isolation):

- **`PGHOST` unset for a direct backup invocation** (P1): reproduced the
  documented pre-release-backup command
  (`docker compose exec -T backup /scripts/backup_postgres.sh`, no
  `PGHOST` set) failing with `could not connect to server: No such file or
  directory` (local socket), matching the review's description exactly.
  After defaulting `PGHOST=database` in the script, the same command
  succeeded.
- **World-readable dumps** (P2): confirmed the pre-fix dump was
  `-rw-r--r--` (0644); after adding `umask 077`, a fresh dump was
  `-rw-------` (0600).
- **Malformed alert JSON** (P1): confirmed the pre-fix payload embedding a
  readiness-endpoint JSON body (with its own embedded double quotes) was
  not valid JSON; after JSON-escaping the message, the same input produced
  a payload that parsed cleanly with `python3 -c "import json; ..."`.
- **`$POSTGRES_DB` expanding on the host, not the container** (P1):
  reasoned fix (host shell has no `POSTGRES_DB`; only `env_file:` injects
  it into the container) -- corrected the documented command to
  `sh -c '... "$POSTGRES_DB"'` so it expands inside the container.
- **ntfy.sh payload-schema mismatch** (P2): narrowed the documented
  `BBBFFL_ALERT_WEBHOOK_URL` claim to the one schema actually implemented
  (Slack-compatible `{"text": ...}`), rather than falsely advertising
  compatibility with a destination whose JSON schema differs.
- **Cron child failures invisible in container status** (P2): confirmed by
  reasoning (PID 1 is `crond`, unaffected by a child job's exit code) --
  addressed by adding a real `HEALTHCHECK`
  (`deploy/production/scripts/check_backup_freshness.sh`) to the `backup`
  service. Rehearsed all three states directly:
  ```
  $ docker compose exec -T backup /scripts/check_backup_freshness.sh   # no backup yet
  no successful backup in /backups newer than 26h
  exit=1
  $ docker compose exec -T backup /scripts/backup_postgres.sh          # take one
  ...backup succeeded...
  $ docker compose exec -T backup /scripts/check_backup_freshness.sh   # now fresh
  exit=0
  $ docker compose exec -T backup sh -c 'BBBFFL_BACKUP_MAX_AGE_HOURS=0 /scripts/check_backup_freshness.sh'
  no successful backup in /backups newer than 0h
  exit=1
  ```
  and confirmed `docker compose ps`/`docker inspect .State.Health` reflect
  it: `Up ... (health: starting)` before the first backup (the 26h
  `start_period` means Docker reports `starting`, not `unhealthy`, during
  that initial window -- an operator would still notice a container stuck
  in `starting` well past its first expected backup), then
  `Up ... (healthy)` immediately after one succeeds.

## I. Third review pass: a genuine schema-drift bug, reproduced and fixed

The third Codex pass found a real correctness bug in `restore_postgres.sh`
that section E's rehearsal had not exercised (it only added a data row, no
schema change): `pg_restore --clean --if-exists` only drops objects present
in the *archive being restored*, so a table/sequence a later migration
introduced would survive a restore to an older backup even as
`alembic_version` goes back to that older revision -- exactly the
condition a database-affecting rollback creates on purpose. Reproduced the
bug directly before fixing it:

```
$ psql -d bbbffl -c "CREATE TABLE pre_release_table (id int primary key, note text); INSERT INTO pre_release_table VALUES (1, 'pre-release data');"
$ backup_postgres.sh                                    # pre-release backup
$ psql -d bbbffl -c "CREATE TABLE post_release_table (id int primary key); CREATE SEQUENCE post_release_seq;"   # simulated bad release

$ pg_restore --clean --if-exists --no-owner --dbname=bbbffl <pre-release-backup>   # the OLD approach
$ psql -d bbbffl -c "\dt"
 public | post_release_table | table   <- BUG: survives the restore
 public | pre_release_table  | table
```

Fixed `restore_postgres.sh` to drop and recreate the target database
before restoring into it, rather than relying on `pg_restore --clean`.
Re-ran the identical scenario against the fixed script:

```
$ BBBFFL_ALLOW_RESTORE_OVER_LIVE_DATABASE=yes restore_postgres.sh <pre-release-backup> bbbffl
...dropping existing target database 'bbbffl' for a clean restore
...creating target database 'bbbffl'
...restore into 'bbbffl' completed
$ psql -d bbbffl -c "\dt"
 public | pre_release_table  | table   <- post_release_table and its sequence are gone; fix confirmed
$ psql -d bbbffl -c "SELECT * FROM pre_release_table;"
 1 | pre-release data                  <- pre-release data intact
```

The same pass also found that `lib_alert.sh` always used `wget`, but
`readiness_watch.sh` (host-run) documents `curl` as its prerequisite --
a host with `curl` and no `wget` would silently fail to deliver every
alert. Fixed `lib_alert.sh` to try `curl` first, falling back to `wget`,
and confirmed both paths select correctly: inside the Alpine backup
container (`curl` absent, `wget` present) it used `wget`; on this host
(`curl` present) it used `curl`. Both attempts against a deliberately
unreachable port failed with a normal connection-refused error, not a
missing-tool error.

## J. Fourth review pass: authenticated readiness probe and network/credential isolation

A fourth Codex pass found two more P1s and one P2, all fixed and
re-verified against a real four-service stack (`app`, `database`,
`backup`, `proxy`) brought up together for the first time in this
rehearsal:

- **Readiness probed an unauthenticated endpoint** (P1): `check_connectivity`
  originally hit the bare `GET /api/{version}` discovery route, which
  needs no `AFL_API_KEY` at all -- a missing/expired/rejected key would
  leave `/health/ready` reporting `"ok"` while every real afl-api request
  BBBFFL depends on returns 401. Changed it to `GET /api/{version}/seasons`
  (a small, already-required, credential-checked endpoint) and added
  `tests/test_afl_client.py::test_check_connectivity_fails_when_the_api_key_is_rejected`
  plus a test pinning the exact path hit, so a future regression back to
  the unauthenticated route would fail CI.
- **Uvicorn did not trust Caddy's forwarded headers** (P1): confirmed via
  `docker inspect` that Caddy's own container IP (`172.18.0.3` in this
  rehearsal) differs from what `app`'s access log showed for a request
  proxied through it (`172.18.0.1` -- the address Caddy itself observed and
  forwarded, not its own address), proving `--forwarded-allow-ips=*` makes
  Uvicorn use the real forwarded client address rather than always
  reporting Caddy's own IP for every request. Without this fix, every
  coach's login attempt would have collapsed into the same
  `LoginRateLimiter` bucket (keyed by `request.client.host`), so five
  failed logins from any one of them would lock out all of them.
- **The internet-facing proxy had the full secrets file and network path
  to the database** (P2): removed `env_file:` from the `proxy` service
  entirely (Caddy needs zero application settings -- the domain is now a
  literal value in `deploy/production/Caddyfile`, not `{$BBBFFL_DOMAIN}`
  substituted from `.env.production`) and split the compose network into
  `frontend` (`app`, `proxy`) and `backend` (`app`, `database`, `backup`).
  Verified both directly: `docker compose exec proxy env` shows none of
  `POSTGRES_PASSWORD`/`BBBFFL_ADMIN_TOKEN`/`BBBFFL_SESSION_SECRET`/
  `AFL_API_KEY`, and `docker compose exec proxy wget ... http://database:5432`
  fails to even resolve the hostname (`NXDOMAIN`) -- proxy has no network
  path to the database at all, not merely no credential for it.

## K. Fifth review pass: backup/restore serialization and a config-validation gap

A fifth Codex pass found two more P2s:

- **A scheduled backup could race a live database-affecting rollback**:
  the documented rollback procedure stopped `app` but left `backup`'s
  cron running, so a scheduled `pg_dump` firing mid-restore could hold a
  connection that makes `dropdb` fail, or archive a database
  `restore_postgres.sh` had only half-rebuilt. Fixed the documented
  procedure to stop `backup` too, run the restore from a disposable
  one-off container (`docker compose run --rm --entrypoint sh backup -c
  '...'` -- overriding the entrypoint, since the service's own
  `backup_entrypoint.sh` ignores any command passed to it and always
  starts `crond`), then restart the real `backup` service afterward.
  Rehearsed the exact corrected command sequence against a real database:
  stopped `backup`, ran the one-off restore container successfully
  (`exit=0`, same drop/create/restore behaviour as section E/I), then
  confirmed `backup` restarts cleanly afterward.
- **`BBBFFL_READINESS_TIMEOUT_SECONDS` accepted `inf`/`nan`**: `float()`
  parses both, and the existing `<= 0` check does not reject either
  (`inf > 0`, and every comparison with `nan` is `False`). Added a
  `math.isfinite()` check alongside the positivity check, and parametrized
  regression tests for `inf`, `-inf` and `nan`.

## L. Sixth review pass: a public placeholder secret and an unbounded connect

A sixth Codex pass found one more P1 and one more P2:

- **The checked-in `.env.production.example` placeholder could satisfy
  production's admin-token/session-secret requirements** (P1): both
  fields previously held `CHANGE-ME-...` values so an operator who copied
  the file without editing them would still pass `get_settings()`'s
  "is it set" check -- silently running production with a secret that is
  public in the repository's history. Fixed on both sides: `app/config.py`
  now rejects the literal value `CHANGE-ME` outright for both
  `BBBFFL_ADMIN_TOKEN` and `BBBFFL_SESSION_SECRET` (a new
  `_EXAMPLE_PLACEHOLDER_SECRET` constant, checked the same way the
  existing `_DEV_SESSION_SECRET` placeholder already is), and
  `.env.production.example` now leaves both fields empty so a forgotten
  value fails closed on the "required, but missing" branch even before
  the placeholder check would apply. Verified with two new regression
  tests (`test_production_refuses_the_checked_in_example_admin_token_placeholder`,
  `test_production_refuses_the_checked_in_example_session_secret_placeholder`)
  confirming `get_settings()` raises `SettingsError` for `CHANGE-ME` in
  either field.
- **A blocking PostgreSQL `connect()` had no timeout of its own** (P2):
  `GET /health/ready`'s database probe (`app/routes/health.py`) bounds
  its own *awaiting* coroutine with `asyncio.wait_for`, but that cannot
  stop an underlying blocking `engine.connect()` call that is still
  establishing a fresh TCP connection -- under a network partition to
  PostgreSQL, the readiness request would return its timeout response,
  but the worker thread (and the pooled connection slot it was trying to
  fill) would stay blocked until the OS-level TCP timeout eventually
  gave up, on the order of minutes. Fixed by passing
  `connect_args={"connect_timeout": 10}` (psycopg/libpq's own
  connect-phase timeout, in seconds) for every non-SQLite `connect()`
  call in `app/db.py` -- this bounds only the connect phase, never a
  query already in flight, and 10s is far below
  `BBBFFL_READINESS_TIMEOUT_SECONDS`'s own default.

  Rehearsed against a disposable `postgres:16-alpine` container
  (`docker run --name bbbffl_rehearsal_pg -e POSTGRES_DB=bbbffl -e
  POSTGRES_USER=bbbffl -e POSTGRES_PASSWORD=rehearsalpass -p
  15432:5432 postgres:16-alpine`):

  ```
  >>> db = connect("postgresql+psycopg://bbbffl:rehearsalpass@localhost:15432/bbbffl")
  >>> db.execute("SELECT 1").fetchone()
  normal connect+query result: {'?column?': 1}

  >>> start = time.monotonic()
  >>> connect("postgresql+psycopg://bbbffl:rehearsalpass@10.255.255.1:5432/bbbffl").execute("SELECT 1")
  unreachable-host connect failed after 10.0s: OperationalError: (psycopg.errors.ConnectionTimeout) connection timeout expired
  ```

  A normal connection still succeeds unchanged with `connect_timeout=10`
  set, and a connection attempt to an unreachable (black-hole, non-
  responding) address now fails at exactly the configured 10s bound
  instead of hanging -- confirming the fix closes the gap without
  affecting ordinary connections. Container removed immediately after
  (`docker rm -f bbbffl_rehearsal_pg`).

## M. Seventh review pass: a destructive restore of a bad dump, and dead peers on established connections

A seventh Codex pass, re-reviewing the section L fixes, found one more P1
and one more P2:

- **A corrupted/truncated dump would destroy the live target before
  failing** (P1): section I's fix made `restore_postgres.sh` drop and
  recreate the target database before calling `pg_restore`, to guarantee
  no schema-drift leftovers survived a restore. That fix did not account
  for the archive itself being bad: a truncated, corrupted, or wrong-
  format dump file would only be discovered by `pg_restore` *after* the
  target had already been dropped and recreated empty, turning a bad
  backup file into a second, self-inflicted outage. Fixed by validating
  the archive with `pg_restore --list` (which parses the custom-format
  archive's table of contents without connecting to any database) before
  the drop, refusing outright if that fails.

  Rehearsed against a real database, reproducing the exact failure mode
  first: created a target database with its own pre-existing data
  (`existing_target_data`), then ran the restore script against a
  deliberately truncated dump (`head -c 200` of a real backup):

  ```
  2026-09-27T05:19:34Z [ERROR] dump file '/backups/corrupt-test.dump' failed pg_restore --list validation (truncated, corrupted, or not a pg_dump custom-format archive) -- refusing to touch 'bbbffl_restore_target'
  exit code: 1
  ```

  The target database's pre-existing data survived untouched
  (`SELECT * FROM existing_target_data` still returned its one row
  afterwards). Then confirmed a legitimate restore is unaffected by the
  new check: the same script, given the real (untruncated) dump, still
  dropped, recreated, and restored the target successfully (`exit=0`,
  correct data present afterwards).

- **`connect_timeout` does nothing for a connection already established
  and pooled** (P2): the section L fix only bounds the TCP-connect phase.
  A query issued on a connection whose peer has since become unreachable
  (not merely refused -- a genuine network partition, with no RST or FIN)
  has no libpq-level timeout of its own, and without TCP keepalive
  enabled, Linux's default `tcp_keepalive_time` of two hours means that
  worker thread and pooled slot could stay blocked for hours, not
  seconds. Fixed by enabling TCP keepalive on every pooled PostgreSQL
  connection (`keepalives=1, keepalives_idle=10, keepalives_interval=5,
  keepalives_count=4` in `app/db.py`'s `connect_args`), bounding
  dead-peer detection to roughly 30 seconds instead of hours. This is a
  socket-level dead-peer detector, not a query deadline: it never
  interrupts a query that is genuinely still running against a server
  that is still there, however long that legitimately takes, so it
  cannot cut off a slow migration or report query.

  A true network-partition rehearsal (dropping packets between an
  established client and server with no RST/FIN) needs host-level
  firewall rules broad enough to risk this sandbox's own networking, so
  instead verified the fix is actually applied where it matters: opened a
  connection through `app.db.connect()` against a disposable
  `postgres:16-alpine` container, extracted the raw OS socket the pooled
  connection was using, and read its actual `SO_KEEPALIVE`/`TCP_KEEPIDLE`/
  `TCP_KEEPINTVL`/`TCP_KEEPCNT` socket options directly:

  ```
  SO_KEEPALIVE=1 TCP_KEEPIDLE=10 TCP_KEEPINTVL=5 TCP_KEEPCNT=4
  query still works normally: {'?column?': 1}
  ```

  This confirms libpq actually applied the configured values to the
  kernel socket (rather than silently ignoring an unrecognized
  connection-string parameter) and that an ordinary query still succeeds
  unchanged with keepalive enabled.

## N. Eighth review pass: data-block corruption and a stalled (not dead) backend

An eighth Codex pass, re-reviewing the section M fixes, found the same two
underlying gaps had not yet been fully closed:

- **`pg_restore --list` cannot catch corruption in the archive's data,
  only its table of contents** (P1): section M's fix validated the
  archive before dropping the target, but `--list` only parses the
  header/TOC -- a dump truncated or corrupted further in (inside a
  table's actual data) would still pass that check and only fail once
  the real restore reached that point, which, under section M's design,
  was already running directly against the (already-dropped) target.
  Fixed by restoring into a fresh, uniquely-named temporary database
  first (`<target>__bbbffl_restore_tmp`), and only replacing the named
  target -- drop, then a fast catalog-only rename of the temporary
  database in as the target -- once that restore has fully succeeded. A
  `pg_restore` failure at any point, including deep in the data, now
  never touches the target at all; a stale temporary database left by a
  previous crashed/killed attempt is dropped and recreated fresh on the
  next run, making the script safe to simply re-run.

  Reproduced the exact failure mode the review described, not just the
  header/TOC corruption section M's rehearsal covered: took a real
  ~560KB backup of a database with 20,000 bulk rows, then flipped bytes
  at 90% through the file -- deep in the compressed data, well past the
  table of contents:

  ```
  --- confirm pg_restore --list still succeeds (TOC intact) ---
  pg_restore --list exit code: 0
  ```

  Confirming the review's exact point: the corrupted file still passes
  `--list`. Then ran the actual restore script against it, against a
  target database holding its own pre-existing data:

  ```
  2026-09-27T05:33:58Z [INFO] creating temporary database 'bbbffl_restore_target__bbbffl_restore_tmp' for the restore
  pg_restore: error: could not uncompress data: incorrect data check
  2026-09-27T05:33:59Z [CRITICAL] PostgreSQL restore of /backups/deep-corrupt-test.dump into temporary database 'bbbffl_restore_target__bbbffl_restore_tmp' FAILED -- 'bbbffl_restore_target' was never touched
  exit code: 1
  ```

  The target's pre-existing data survived untouched, and no leftover
  temporary database remained afterward. Then confirmed the legitimate
  path is unaffected: the same script, given the real (uncorrupted) dump,
  restored into the temporary database and swapped it in successfully
  (`exit=0`, correct 20,000-row table present, old target data gone --
  the full replace-on-success contract preserved). Finally re-rehearsed
  the database-affecting rollback path itself (`BBBFFL_ALLOW_RESTORE_
  OVER_LIVE_DATABASE=yes`, target equal to `PGDATABASE`) against the new
  swap logic end-to-end, confirming it still restores the live database
  correctly.

- **TCP keepalive detects a dead network peer, not a live one that stalls**
  (P2): section M's keepalive fix bounds a query stuck because the
  *network* partitioned, but does nothing when the PostgreSQL host is
  still reachable and answering keepalive probes while its backend
  simply never finishes the query (lock contention, resource
  starvation) -- exactly the scenario `asyncio.wait_for` already returns
  a 503 for without freeing the underlying worker/connection. Fixed by
  giving the readiness probe's own query a PostgreSQL `statement_timeout`
  scoped to just that one call: `app.db.DatabaseConnection.
  execute_bounded` issues `SET LOCAL statement_timeout = <ms>` (from the
  same `readiness_timeout_seconds` value the probe is already bounded
  by) immediately before the `SELECT 1`, on the same short-lived pooled
  connection -- `SET LOCAL` is transaction-scoped, so it reverts the
  moment that connection is released and can never leak onto whatever
  query a later, unrelated caller runs on the same pooled connection.
  SQLite (dev/tests) has no equivalent setting, so the timeout is simply
  not applied there -- the query still runs normally.

  Rehearsed against a real disposable PostgreSQL container with three
  new regression tests (`tests/test_db_connection_lifecycle_postgresql.py`,
  gated on `BBBFFL_DATABASE_URL` like the repository's other
  `*_postgresql.py` suites): a `pg_sleep(1)` query with
  `timeout_seconds=0.1` is aborted by PostgreSQL itself
  (`canceling statement due to statement timeout`); an ordinary fast
  query with a 5s timeout is unaffected; and, most directly addressing
  the "never leak" requirement, a `pg_sleep(1)` call that times out is
  immediately followed by a genuinely slow (`pg_sleep(0.3)`) but
  legitimate query run with *no* timeout at all on the connection pool,
  which completes normally -- proving the prior call's `statement_timeout`
  did not survive onto it. All three tests pass against a real
  `postgres:16-alpine` container.

## What this rehearsal does not prove

- It does not prove the real production afl-api deployment is reachable or
  contract-compatible -- that remains the separate, already-tracked
  outstanding item (`docs/afl-api-v1-contract.md`, and
  `docs/2027-live-season-readiness.md`'s live afl-api row), and this issue
  does not claim otherwise.
- It does not prove a real Let's Encrypt certificate issuance against a
  real public domain -- the Caddyfile's `tls internal` rehearsal
  alternative was used instead (see `docs/production-operations.md`'s
  "What remains environment-specific").
- It does not prove backup/restore/rollback timing at production data
  volumes -- the rehearsal database was small (representative, not
  full-season-scale). The RPO/RTO targets proposed in
  `docs/production-operations.md` account for this topology's actual
  behaviour, not a load test.
- It is a disposable rehearsal in the implementation environment, not a
  staging rehearsal on Steve's real production host -- see
  `docs/2027-live-season-readiness.md` for how this issue's completion is
  classified there.
