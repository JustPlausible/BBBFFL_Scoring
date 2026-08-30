# Round 1 rehearsal operator runbook

This is the supported **Docker Compose-first** procedure for a persistent
Round 1 rehearsal/home-server deployment. It needs Docker with the Compose
plugin and a repository checkout; it does **not** need host Python. Use the
separate [source/developer workflow](../bbbffl_app/README.md#source-development-workflow)
when changing or testing application code.

All commands below run from the repository root and use the explicit Compose
project name `bbbffl-round1-rehearsal`. Keep that name: it isolates containers,
networks, and volumes from historical Grand Final and other BBBFFL deployments
on the same host.

## Clean-host sequence

1. Create the rehearsal configuration. The example deliberately selects
   development (replay is refused in production), the Compose PostgreSQL
   database, replay mode, and the authoritative evidence path:

   ```bash
   cp bbbffl_app/.env.rehearsal.example bbbffl_app/.env.rehearsal
   # Review secrets before exposing port 8000 beyond a trusted private host.
   ```

   The evidence setting must remain an exact filename match:

   ```env
   BBBFFL_AFL_MODE=replay
   BBBFFL_AFL_REPLAY_EVIDENCE_PATH=/replay/evidence/round1-2026-rehearsal-evidence.json
   ```

2. Build the normal production application image, then start only the
   rehearsal database required by bootstrap/migrations:

   ```bash
   docker compose -p bbbffl-round1-rehearsal build app
   docker compose -p bbbffl-round1-rehearsal up -d database
   ```

3. Bootstrap the migrated database and generate evidence in the shared
   evidence volume:

   ```bash
   docker compose -p bbbffl-round1-rehearsal run --rm \
     -v "$PWD/bbbffl_app:/app" \
     app python -m scripts.bootstrap_round1_2026 \
     --database-url postgresql+psycopg://bbbffl:rehearsal-only@database/bbbffl_round1_rehearsal \
     --evidence-path /replay/evidence/round1-2026-rehearsal-evidence.json
   ```

   The bootstrap performs migrations before seeding and prints Coach A's
   rehearsal credential and browser identifiers. `scripts/` is deliberately
   excluded from the production runtime image; this one-off container mounts
   the checkout at the image's `/app` working directory so the bootstrap module
   is available without changing that production-image boundary. The named
   evidence volume remains mounted at `/replay/evidence` by the Compose service.

4. Start the persistent application after bootstrap succeeds:

   ```bash
   docker compose -p bbbffl-round1-rehearsal up -d app
   ```

5. Sign in at <http://localhost:8000/login> with the printed Coach A
   credential. Use `/account` to reach the lineup, submit Coach A's nine-player
   lineup, then use `/scorer/round-centre` to advance, calculate, review, and
   publish Round 1. The public season link displays results and the ladder.

## Operator verification

Run these checks from the repository root:

```bash
# Both database and app should be running/healthy.
docker compose -p bbbffl-round1-rehearsal ps

# The generated file must exist at the exact path visible to the app.
docker compose -p bbbffl-round1-rehearsal exec app \
  test -f /replay/evidence/round1-2026-rehearsal-evidence.json

# The configured value must print the identical path.
docker compose -p bbbffl-round1-rehearsal exec app \
  printenv BBBFFL_AFL_REPLAY_EVIDENCE_PATH

# Startup should be successful; the health endpoint should answer.
curl --fail http://localhost:8000/health

# Startup diagnostics must identify afl_mode=replay (not live).
docker compose -p bbbffl-round1-rehearsal logs app | grep 'afl_mode=replay'
```

Replay is **fail-closed**. Missing, malformed, or incorrectly configured
evidence is expected to prevent application startup; the application must not
silently switch to live AFL data. This is a safety property, not a reason to
change replay mode to live. If startup reports missing evidence, check, in
order: (1) the generated filename, (2) the evidence volume mount, (3)
`BBBFFL_AFL_REPLAY_EVIDENCE_PATH`, and (4) whether the file is visible at that
exact path in a one-off container:

```bash
docker compose -p bbbffl-round1-rehearsal run --rm --no-deps app \
  test -f /replay/evidence/round1-2026-rehearsal-evidence.json
```

## Stop, restart, and safely reset

A normal stop preserves the dedicated database and evidence volumes; start it
again with `up -d`:

```bash
docker compose -p bbbffl-round1-rehearsal stop
docker compose -p bbbffl-round1-rehearsal up -d
```

For a genuinely clean rehearsal, the following is intentionally destructive:
it removes **only volumes belonging to the explicitly named
`bbbffl-round1-rehearsal` Compose project**, then recreates only its database.
It does not prune Docker or address containers/volumes from any other Compose
project. Re-run the bootstrap and application-start steps above afterward.

```bash
docker compose -p bbbffl-round1-rehearsal down --volumes
docker compose -p bbbffl-round1-rehearsal up -d database
```

Never substitute broad `docker system prune`, unscoped volume deletion, or a
historical BBBFFL project name on a shared server.
