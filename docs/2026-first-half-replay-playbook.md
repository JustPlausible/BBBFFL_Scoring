# Authoritative 2026 first-half replay playbook

## A. Purpose and scope

This is the authoritative Milestone B½ historical BBBFFL Rounds 1–9 operational replay. It supersedes earlier R1–R9 procedural fragments where they conflict. It acquires real AFL Opening Round and Rounds 1–9 evidence once, then uses the normal lineup, lockout, calculation, review, official-result and ladder services with no AFL network. `round1-rehearsal.md` remains a separate **synthetic browser rehearsal**; hermetic automated tests are a third, software-validation workflow. This run ends in the official state immediately before future mid-season-draft work.

## B. Checkout and commit verification

```bash
git clone https://github.com/JustPlausible/BBBFFL_Scoring.git
cd BBBFFL_Scoring
git fetch --all --tags --prune
git switch <branch-being-tested>
git status --porcelain=v1                 # must print nothing
COMMIT=$(git rev-parse HEAD); printf '%s\n' "$COMMIT" | tee replay-commit.txt
git branch --show-current
docker --version
docker compose version
```

Record the actual commit and built image digest in the replay log; never substitute a predicted merge hash.

## C. Acquire and validate evidence

Acquisition is the only phase requiring the configured consumer API and `AFL_API_KEY`. It automatically finds the year (not a database ID), Opening Round and rounds 1–9, all matches, final statistics, identifiers, byes, and optional rosters. Do not prepare Bruno files.

```bash
mkdir -p replay/2026-first-half/{evidence,state,logs,backups,config}
cd bbbffl_app
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export AFL_API_BASE_URL=https://<consumer-api-host>
read -rsp 'AFL API key: ' AFL_API_KEY; echo; export AFL_API_KEY
python -m scripts.first_half_replay acquire \
  --output ../replay/2026-first-half/evidence/2026-first-half.json \
  --player-pool-output ../replay/2026-first-half/evidence/2026-player-pool.json
unset AFL_API_KEY
python -m scripts.first_half_replay checkpoint \
  --state ../replay/2026-first-half/state/checkpoint.json \
  --effective-at 2026-01-01T00:00:00Z --stage scheduled
python -m scripts.first_half_replay validate \
  --evidence ../replay/2026-first-half/evidence/2026-first-half.json \
  --state ../replay/2026-first-half/state/checkpoint.json
cd ..
```

Expect validation `PASS`, season 2026, ten included AFL round identities, every enumerated match with stat coverage, and explicit available/unavailable roster coverage. Inspect `manifest` for package/schema, UTC acquisition time, source host (never credentials), API/exporter versions and identifiers. A missing optional roster is diagnostic, not fabricated. Missing stats, identifiers, malformed time, inconsistent round/match, or unsupported schema is fatal.

**Hermetic proof:** now stop/disconnect `afl-api` (or firewall the host). Leave it unavailable for every remaining step. Replay never falls back to it.

## D. Dedicated installation, lifecycle and safety

This installation is deliberately named `bbbffl-2026-first-half`, listens at <http://localhost:8018/login>, uses its own `first-half-database` project volume, and bind-mounts `replay/2026-first-half/{evidence,state,logs,backups}`. It cannot reuse the synthetic Round 1 database.

```bash
cp bbbffl_app/.env.first-half-replay.example bbbffl_app/.env.first-half-replay
# Set unique session/admin/operator secrets; set no live AFL endpoint.
COMPOSE='docker compose -p bbbffl-2026-first-half -f compose.first-half-replay.yaml'
$COMPOSE config                         # inspect names and exact mounts
$COMPOSE build
$COMPOSE run --rm app alembic upgrade head
$COMPOSE up -d
$COMPOSE ps
docker compose -p bbbffl-2026-first-half -f compose.first-half-replay.yaml logs app
curl --fail http://localhost:8018/health
```

Restart preserves PostgreSQL, evidence, and checkpoint: `$COMPOSE restart app`. Stop/start with `$COMPOSE stop` and `$COMPOSE start`. Changing the checkpoint file is persisted across application restarts; restart `app` after changing it so the eagerly loaded replay boundary sees the new state. Removing evidence causes controlled startup failure. Retaining evidence while resetting the database does not alter evidence.

Backup before each round:

```bash
$COMPOSE exec -T database pg_dump -U bbbffl bbbffl_2026_first_half \
  > replay/2026-first-half/backups/after-round-N.sql
$COMPOSE logs --no-color > replay/2026-first-half/logs/after-round-N.log
cp replay/2026-first-half/state/checkpoint.json replay/2026-first-half/backups/checkpoint-after-round-N.json
```

Destructive reset is intentionally explicit and project-scoped: back up, run `$COMPOSE down`, verify `docker volume ls | grep bbbffl-2026-first-half`, then only if abandonment is approved run `$COMPOSE down -v`. Never use an unqualified volume deletion. Evidence bind mounts remain; deliberate evidence removal makes the next startup fail closed.

## E. Supported first-half bootstrap

Copy `config/replay/2026-first-half.template.json` to `replay/2026-first-half/config/2026-first-half.json`, enter genuine accepted identities/rules/order, and point `player_pool_file` at `/replay/evidence/2026-player-pool.json`. This directory is mounted read-only at `/secure`. Do not invent coach facts or use SQL. Then:

```bash
$COMPOSE run --rm -v "$PWD/bbbffl_app:/app" app \
  python -m scripts.bootstrap_2026_first_half --config /secure/2026-first-half.json
read -rsp 'Replay operator password: ' BBBFFL_REPLAY_OPERATOR_PASSWORD; echo; export BBBFFL_REPLAY_OPERATOR_PASSWORD
$COMPOSE run --rm -v "$PWD/bbbffl_app:/app" -e BBBFFL_REPLAY_OPERATOR_PASSWORD app \
  python -m scripts.bootstrap_2026_first_half \
  --config /secure/2026-first-half.json --provision-operator
unset BBBFFL_REPLAY_OPERATOR_PASSWORD
$COMPOSE run --rm -v "$PWD/bbbffl_app:/app" app python -m scripts.bootstrap_2026_first_half \
  --config /secure/2026-first-half.json --readiness-only --json
```

Require `READY` and verify 2026 rules, ordinary competition, ten genuine coach/team identities, Administrator grant/acting contexts, BBBFFL rounds 1–9, provider/year/player count, squad limit, accepted ten-position draft order, no completed pick, and Draft Board next action **Pick 1**. See `2026-first-half-replay-bootstrap.md` for conflict diagnostics.

## F. Preseason browser workflow

1. Sign in at `/login` with the provisioned Administrator and verify the active Administrator role/acting context in **Season Centre**.
2. Open **Draft Board**, make each genuine pick in order, and finalise the draft using its supported action.
3. In Season Centre/Draft Board verify all ten squads and the configured squad limit.
4. Exercise only historically required supported **Preseason Trades**; never reconstruct with SQL.
5. Freeze opening squads through the existing season lifecycle control.
6. Open **Fixture Draw**, conduct the fixture-number draw, and record its audit event.
7. Preview all R1–R9 fixtures; require five matchups each.
8. Explicitly accept/freeze the draw. Capture the UI/audit checkpoint and database backup.

## G. Canonical procedure for each BBBFFL round 1–9

Historical exports commonly have only scheduled starts and final facts. That is valid: before a scheduled start the match is unlocked subject to configured BBBFFL triggers; at the instant of start it is started/locked; final statistics and `CONCLUDED` appear only at `final-results`. Never manufacture `LIVE`, quarters, halftime, or `POSTGAME`.

For each round:

1. Record commit/image and evidence package/version.
2. Stage a timezone-aware pre-lockout instant with `$COMPOSE run --rm -v "$PWD/bbbffl_app:/app" -v "$PWD/replay/2026-first-half/state:/replay/state" app python -m scripts.first_half_replay checkpoint --state /replay/state/checkpoint.json --effective-at <UTC-ISO> --stage scheduled`; restart app. The source mount supplies the intentionally image-excluded operator script, while the writable one-off state mount overrides the application's read-only state mount without changing the runtime container.
3. Verify the persisted JSON and replay time/checkpoint shown in diagnostics/logging.
4. Verify authenticated role and acting context.
5. Open **Round Preflight**; verify five fixtures, accepted AFL mapping, and selective/main trigger configuration.
6. Inspect **Opening Round Operations** deferred selections where applicable, pass readiness, then **Open Round**.
7. Through **Weekly Lineup**, create, carry forward, or Administrator-proxy all ten lineups using only supported actions; record submission/proxy provenance.
8. Repeat the checkpoint command at each relevant scheduled match start and main boundary, restarting app each time. Confirm later-club edits remain possible and started/triggered edits are rejected.
9. Stage finality with the same source-mounted command plus `--effective-at <after-final-UTC> --stage final-results --round-id <mapped-AFL-round-id>`; restart. The command retains all previously released round IDs and refuses to move effective time backwards. Verify only the released round becomes `CONCLUDED` with final stats; future rounds remain hidden.
10. In **Scorer Round Review**, calculate/refresh all scores; inspect DNP, interchange and availability cases. Resolve only legitimate scorer/manual-review cases.
11. Move to review, sign off, and publish all five results through existing controls.
12. Verify public **Round Centre**, results, and cumulative ladder immediately.
13. Inspect result versions/corrections and audit provenance. Record discrepancies; never change evidence to force agreement.
14. Back up database, checkpoint metadata and logs before continuing.

## H. Round-specific notes

* **Opening Round / Round 1:** Opening Round club participation is acquired AFL fact. Coach deferred nominations are operator/competition state and must be entered through **Opening Round Operations** with provenance; never infer them from player scores.
* **Compensating byes:** use the accepted Opening Round rules and acquired round `byes`/identities. A deferred slot uses the existing Opening Round domain and ordinary scoring service.
* **Selective/early lockout:** use the accepted Round Preflight trigger plan and exact acquired starts. Do not assume all players lock at the first match.
* For rounds without richer genuine snapshots, scheduled boundaries plus `final-results` are complete and expected.

## I. Replay test log (leave results blank)

| BBBFFL round | Replay date/time | Operator | Commit/image | Evidence package/version | Effective time/checkpoint | Mapping | Lockout | 10 submissions | Scoring | Sign-off/publish | Ladder checkpoint | Pass/fail | Notes | GitHub issues |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | | | | | | | | | | | | | | |
| 2 | | | | | | | | | | | | | | |
| 3 | | | | | | | | | | | | | | |
| 4 | | | | | | | | | | | | | | |
| 5 | | | | | | | | | | | | | | |
| 6 | | | | | | | | | | | | | | |
| 7 | | | | | | | | | | | | | | |
| 8 | | | | | | | | | | | | | | |
| 9 | | | | | | | | | | | | | | |

## J. Defect handling

Record genuine discrepancies with package ID, checkpoint, screenshots/logs and audit identities; open/link GitHub issues. Do not edit historical evidence to obtain agreement, bypass validation/sign-off, repair accepted state with ad-hoc SQL, invent a lineup, or weaken authentication. A genuine prerequisite defect is a blocking defect, not permission to add replay-only behaviour.

## K. Hard Round 9 exit gate

Do not declare B½ successful until all are checked:

- [ ] Nine BBBFFL rounds are official and all 45 results published.
- [ ] The cumulative ladder reconciles coherently after Round 9.
- [ ] Immutable result versions/correction histories remain accessible.
- [ ] Ownership/squads and ten coach/team identities are correct.
- [ ] Role grants and acting contexts remain correct.
- [ ] Opening Round deferred-selection and compensating-bye provenance is complete.
- [ ] Every DNP/interchange/manual ruling is resolved, or recorded as a blocking defect.
- [ ] No unexpected draft is open and no unexpected lineup draft/submission remains.
- [ ] Round 9 database backup/checkpoint exists.
- [ ] Evidence package, package checksum, logs and operator replay record are retained.
- [ ] The same season/database is ready to continue into the future mid-season draft without rebuilding the competition.
