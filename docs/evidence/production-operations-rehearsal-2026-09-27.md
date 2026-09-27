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
