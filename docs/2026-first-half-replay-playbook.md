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

Player-pool population comes from AFL-api's season-player collection, which is a distinct step from the round/match/stats evidence above:

```
GET /api/v1/seasons
  -> resolve the single season whose year == 2026
  -> GET /api/v1/seasons/{season_id}/players?limit=250&offset=0
  -> GET /api/v1/seasons/{season_id}/players?limit=250&offset=250
  -> ... continues automatically until a page shorter than the requested
     limit (including an empty page) is returned
```

The acquisition command follows every page itself; the operator never manually concatenates pages or prepares a Bruno-captured player file. Every acquired player must carry a resolved requested-season team (AFL-api's `current_team` and any other-season/match-stat team identity are never substituted) and a non-empty `display_name`; an unresolved name or team blocks acquisition instead of being guessed. The captured population is exactly whatever AFL-api has actually persisted into its canonical `competition_season_players` membership at acquisition time -- it is not being claimed as a historically versioned February-2026 snapshot, and membership completeness is bounded by what AFL-api has persisted, not by anything BBBFFL infers.

This player pool requires **no** 2026 Home & Away summary and **no** 2026 StatsPro summary -- a genuine preseason 2026 season member with zero match appearances still belongs in the pool. Do not run a `--build-player-stat-summaries 2026` step before acquisition; none is a prerequisite. (Previous-season H&A statistics may later support a 2027 draft UI/reference workflow; they are out of scope here and are never a prerequisite for this replay.)

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

Expect validation `PASS`, season 2026, ten included AFL round identities, every enumerated match with stat coverage, and explicit available/unavailable roster coverage. Inspect `manifest` for package/schema, UTC acquisition time, source host (never credentials), API/exporter versions and identifiers, plus `player_pool_count`/`player_pool_page_count` for the acquired season-player population and how many pages it took. A missing optional roster is diagnostic, not fabricated. Missing stats, identifiers, malformed time, inconsistent round/match, or unsupported schema is fatal.

**Hermetic proof:** now stop/disconnect `afl-api` (or firewall the host). Leave it unavailable for every remaining step. Replay never falls back to it -- once acquisition has written both output files, nothing later in this playbook depends on AFL-api being reachable.

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

Copy `config/replay/2026-first-half.template.json` to `replay/2026-first-half/config/2026-first-half.json`, enter genuine accepted identities/rules/order, and point `player_pool_file` at `/replay/evidence/2026-player-pool.json`. Also point the config's `opening_round.evidence_file` at the acquired `/replay/evidence/2026-first-half.json` package from step C -- the template already carries the genuine ten-club 2026 Opening Round rule set (issue #126); it is configured/supplied here, never invented, and player nominations are deliberately **not** part of this file. This directory is mounted read-only at `/secure`. Do not invent coach facts or use SQL. Then:

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

Require `READY` and verify 2026 rules, ordinary competition, ten genuine coach/team identities, Administrator grant/acting contexts, BBBFFL rounds 1–9, provider/year/player count, squad limit, accepted ten-position draft order, no completed pick, Draft Board next action **Pick 1**, and the report's `opening_round` section showing `accepted_rule_count: 10`, `complete: true`, and `targets: {"2": 4, "3": 4, "4": 2}` with `nomination_count: 0`. This Opening Round activation is established from the local acquired evidence acquired in step C; AFL-api remains disconnected throughout. See `2026-first-half-replay-bootstrap.md` for conflict diagnostics.

Then, in the browser: sign in as the provisioned Administrator, open **Draft Board** and confirm the next action is **Pick 1**. From **Season Centre**, the Opening Round Operations section is now visible (accepted rules exist); its nomination-progress indicator counts currently owned, rule-eligible players rather than accepted rules, so with no picks made yet it correctly reads `0/0` (reported ready -- there is nothing to complete before any club's player is owned), not `0/10`. Open it from that link and confirm all ten accepted rules are visible with zero nominations. Player nominations are entered later, through that same Opening Round Operations workflow reached from Season Centre, only after the draft has produced actual owned players (see section F, steps 6–7) -- never during bootstrap and never inferred from later results.

## F. Preseason browser workflow

This section is the authoritative operator sequence for everything between
bootstrap and the first round preparation: (1) bootstrap season/rules is
section E, above; (2) complete the preseason draft is steps 1–2 below; (3)
validate/freeze opening squads is steps 3–5; (4) complete Opening Round
reconstructed nominations is step 6; (5) confirm Opening Round nomination
readiness is step 7; (6) prepare/open applicable ordinary BBBFFL rounds is
section G, next.

1. Sign in at `/login` with the provisioned Administrator and verify the active Administrator role/acting context in **Season Centre**.
2. Open **Draft Board**, make each genuine pick in order, and finalise the draft using its supported action.
3. In Season Centre/Draft Board verify all ten squads and the configured squad limit.
4. Exercise only historically required supported **Preseason Trades**; never reconstruct with SQL.
5. Freeze opening squads through the existing season lifecycle control.
6. **Complete Opening Round reconstructed nominations.** From **Season Centre**, open the season, and once the draft has produced owned players use the **Opening Round Operations** action shown there (it links to `/operations/seasons/<season-id>/opening-round`; there is no need to know or type that URL directly). For every represented entry with an owned player from a club covered by an accepted Opening Round rule, choose that player -- the page resolves the matching club rule, compensating AFL round and target BBBFFL round automatically -- pick the target slot, and create the nomination with a replay/reconstructed reason. **Nominations are operator/replay evidence entered from the acquired Opening Round club/bye facts (section C) and the coach's actual drafted squad -- never inferred, guessed, or backfilled from any later AFL statistics or match results.**
7. **Confirm Opening Round nomination readiness.** Still in Season Centre (or on the Opening Round Operations page itself), verify the nomination-progress indicator reads complete (e.g. `10/10`) and ready before continuing -- an incomplete state is a stop-and-fix condition, not a warning to note and proceed past.
8. Open **Fixture Draw**, conduct the fixture-number draw, and record its audit event.
9. Preview all R1–R9 fixtures; require five matchups each.
10. Explicitly accept/freeze the draw. Capture the UI/audit checkpoint and database backup.

## G. Canonical procedure for each BBBFFL round 1–9

Historical exports commonly have only scheduled starts and final facts. That is valid: before a scheduled start the match is unlocked subject to configured BBBFFL triggers; at the instant of start it is started/locked; final statistics and `CONCLUDED` appear only at `final-results`. Never manufacture `LIVE`, quarters, halftime, or `POSTGAME`.

For each round:

1. Record commit/image and evidence package/version.
2. Stage a timezone-aware pre-lockout instant with `$COMPOSE run --rm -v "$PWD/bbbffl_app:/app" -v "$PWD/replay/2026-first-half/state:/replay/state" app python -m scripts.first_half_replay checkpoint --state /replay/state/checkpoint.json --effective-at <UTC-ISO> --stage scheduled`; restart app. The source mount supplies the intentionally image-excluded operator script, while the writable one-off state mount overrides the application's read-only state mount without changing the runtime container.
3. Verify the persisted JSON and replay time/checkpoint shown in diagnostics/logging.
4. Verify authenticated role and acting context.
5. Open **Round Preflight**; verify five fixtures, accepted AFL mapping, and selective/main trigger configuration. Where an accepted Opening Round rule targets this round, Round Preflight itself blocks opening and links back to **Opening Round Operations** if any entry's nomination is still incomplete (section F, steps 6–7 should already have completed this; treat a blocker here as a sign that step was missed, not something to work around).
6. Review the readable locked deferred selections shown under **Opening Round exceptions** (player, club, source/compensating AFL round, target BBBFFL round) for provenance, then **Open Round**.
7. Through **Weekly Lineup**, create, carry forward, or Administrator-proxy all ten lineups using only supported actions; record submission/proxy provenance.
8. Repeat the checkpoint command at each relevant scheduled match start and main boundary, restarting app each time. Confirm later-club edits remain possible and started/triggered edits are rejected.
9. Stage finality with the same source-mounted command plus `--effective-at <after-final-UTC> --stage final-results --round-id <mapped-AFL-round-id>`; restart. The command retains all previously released round IDs and refuses to move effective time backwards. Verify only the released round becomes `CONCLUDED` with final stats; future rounds remain hidden.
10. In **Scorer Round Review**, calculate/refresh all scores; inspect DNP, interchange and availability cases. Resolve only legitimate scorer/manual-review cases.
11. Move to review, sign off, and publish all five results through existing controls.
12. Verify public **Round Centre**, results, and cumulative ladder immediately.
13. Inspect result versions/corrections and audit provenance. Record discrepancies; never change evidence to force agreement.
14. Back up database, checkpoint metadata and logs before continuing.

## H. Round-specific notes

* **Opening Round / Round 1:** Opening Round club participation is acquired AFL fact. The ten accepted 2026 Opening Round rules are already established by bootstrap (issue #126) and visible in **Opening Round Operations** before the draft. Coach deferred nominations remain separate operator/competition state and must be entered through **Opening Round Operations** with provenance only after the draft has produced owned players; never infer them from player scores.
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
- [ ] Opening Round deferred-selection and compensating-bye provenance is complete, and Opening Round Operations' nomination readiness (Season Centre and Round Preflight) showed ready before every dependent round was opened.
- [ ] Every DNP/interchange/manual ruling is resolved, or recorded as a blocking defect.
- [ ] No unexpected draft is open and no unexpected lineup draft/submission remains.
- [ ] Round 9 database backup/checkpoint exists.
- [ ] Evidence package, package checksum, logs and operator replay record are retained.
- [ ] The same season/database is ready to continue into the future mid-season draft without rebuilding the competition.
