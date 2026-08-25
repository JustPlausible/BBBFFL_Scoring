# BBBFFL Grand Final live scoring (2027 prototype)

A small FastAPI service implementing the narrow vertical slice from
`docs/plans/2027-grand-final-prototype-brief.md`:

> two manually configured BBBFFL Grand Final teams -> live `afl-api` v1 data
> -> canonical BBBFFL scoring -> individual player/running team scores ->
> scorer-controlled interchange/DNP handling -> final scorer sign-off.

This is the active, core application source and the primary implementation
target for the 2027 rebuild. The archived Google Apps Script projects in
`legacy/gas/` remain historical, migration, and 2026 replay references; they
are not an alternative target for new features.

## What this is not

Per the brief, this prototype deliberately excludes: Google Forms/Sheets
integration, coach authentication, full-season fixtures, the ladder,
squad-ownership validation of SuperScore/Grand Final selections, historical
migration, projections, and AI commentary. AFL data collection and identity
resolution live entirely in `afl-api`; this service only consumes
`/api/v1`.

SuperScore (see below) is an **experimental, opt-in** extension being
trialled alongside the Grand Final during the same live round -- when not
configured, the application behaves exactly as before.

## Architecture

```
bbbffl_app/
  app/
    afl_client.py   # thin adapter over afl-api /api/v1 (the one seam to
                     # touch if a response field name differs from what's
                     # assumed here -- see the module docstring)
    scoring.py       # canonical BBBFFL scoring formulas (pure functions)
    teams.py         # loads the coach-declared Grand Final team JSON
                      # (read-only); also home to parse_roster(), the
                      # lineup-schema validator shared with superscore.py
    superscore.py     # loads the coach-declared SuperScore entries JSON
                       # (read-only); reuses teams.TeamConfig/parse_roster --
                       # there is no separate SuperScore lineup schema
    db.py            # PostgreSQL/SQLite store for scorer decisions (DNP, interchange,
                      # overrides, finalisation), scoped per competition
                      # instance via competition_key -- separate from
                      # teams.py/superscore.py
    service.py        # orchestration: AFL data + team config + scorer
                       # decisions -> the official scoreboard. The core
                       # build_matchup_state() scores an arbitrary list of
                       # entries and is shared, unmodified in its scoring
                       # logic, by both the Grand Final (2 teams) and
                       # SuperScore (N entries, ranked instead of compared)
    routes/            # health, public (read-only), admin (scorer-gated),
                        # superscore (public + admin, opt-in, 404s when
                        # SuperScore isn't configured)
    templates/           # server-rendered public + admin + superscore
                          # pages, JS polling
  data/
    grand_final_teams.json          # coach-declared selection (edit before use!)
    superscore_teams.example.json   # SuperScore trial entries template --
                                     # copy it, fill in real player IDs, and
                                     # point BBBFFL_SUPERSCORE_CONFIG_PATH at
                                     # the copy to opt in
  tests/
```

Scorer decisions are stored **separately** from the coach-declared team
sheet, matching the brief:

```
AFL stats -> calculated BBBFFL score -> optional scorer override -> effective BBBFFL score
coach selection + AFL facts + scorer decisions -> official BBBFFL score
```

## Scoring rules

Recovered from the legacy Google Apps Script implementation
(`legacy/gas/BBBFFL_Results/fetchBBBFFLResults.js` and
`legacy/gas/BBBFFL_Results/generateLiveBBBFFLMatches.js` on the
`audit/google-apps-script-2026` branch, cross-checked against
`docs/reviews/2025-system-forensic-review.md` section 8, where both
independent legacy implementations agreed):

| Position | Formula |
|---|---|
| Forward (x3) | `6 x goals + behinds` |
| Midfield (x3) | total disposals |
| Ruck | `marks + hitouts` |
| Tackler | `6 x tackles` |
| Interchange | scored as whichever starting position it replaces (never as "Interchange" itself) |

These rules are unchanged from the legacy system. See `app/scoring.py` and
`tests/test_scoring.py`.

## Interchange / DNP / override behaviour

- A scorer marks any of the 9 roster slots (8 starting positions +
  Interchange) as DNP. A DNP starting position becomes **vacant** --
  scored as 0 until resolved.
- A scorer may assign the team's Interchange player to **any one** starting
  position, regardless of whether that position is currently DNP (this
  supports the existing "Thursday-night interchange, promoted later"
  loophole). Only one target position per team at a time.
- The system flags `recommended_interchange: true` on a vacant position
  when the team's interchange hasn't been assigned anywhere yet -- this is
  a recommendation only and never changes the official score by itself.
- A scorer may set a direct point override with a required-in-spirit reason
  on any of the 8 scoring positions. The override changes only the
  *effective* BBBFFL score; `calculated_score` (from AFL stats) is always
  shown alongside it in the admin view.
- All of the above remain editable until the Grand Final is explicitly
  finalised, at which point `/api/admin/*` mutation endpoints return
  `423 Locked`.

See `tests/test_service.py` and `tests/test_api.py`.

## Matchup lifecycle

`LIVE -> AWAITING_SCORER_SIGNOFF -> FINAL`. The transition to
`AWAITING_SCORER_SIGNOFF` happens automatically once every AFL match that
an *active* (non-DNP, non-vacant, non-unnamed) roster position depends on
is `completed` (afl-api `CONCLUDED`). A **named Interchange player counts
here too, even while unassigned to a scoring position** -- it's genuinely
playing a live AFL match right now, even though it isn't yet contributing
to the official score, so its match state must not be ignored when
deciding whether the matchup is still LIVE. A scorer-marked-DNP
Interchange is excluded from this, the same as a DNP starter. Moving to
`FINAL` always requires an explicit `POST /api/admin/finalize` call -- the
system never finalises itself.

A match in afl-api's `POSTGAME` status (siren has sounded, but statistics
are not yet declared final) surfaces as its own `postgame` match/position
state -- distinct from both `live` and `completed`. It is **not** treated
as complete for sign-off purposes: a `postgame` match holds the matchup at
`LIVE` the same way a `live` one does, since AFL stats can still be
corrected before afl-api reports `CONCLUDED`. Only `completed` counts
toward `AWAITING_SCORER_SIGNOFF`.

## Interchange presentation

The public/admin API's `interchange` object exposes, independent of any
current assignment: `match_state` (the player's own underlying AFL match
state: `yet_to_play` / `live` / `postgame` / `completed` / `unnamed`) and
`potential_scores` (`{forward, midfield, ruck, tackler}` -- what their
*current* AFL stats would score at each position, computed via the same
`score_position()` used everywhere else; `null` when there's no AFL stat
line yet rather than an invented zero). Both are informational: they never
affect any team total, and assigning Interchange to a position remains an
explicit scorer decision regardless of which potential score is highest.

The public page renders Interchange as a 9th row per team (after Tackler)
showing this information, plus `→ <position>` when assigned -- but its
points column always shows `—`; the actual contribution appears once, on
the assigned position's own row.

`counts` in the public API deliberately still covers only the 8 scoring
positions per team (unchanged shape/semantics from before this feature) --
Interchange is not folded in as a pretend 9th scoring position. Its state
is available separately via each team's `interchange.match_state`.

## SuperScore (experimental, opt-in)

During the final four rounds of the BBBFFL season, all coaches also compete
in SuperScore: independent entries (not head-to-head matches) compared
directly on total score for that round, with tied top scores standing as
ties (no tiebreaker). SuperScore uses **exactly the same scoring engine,
DNP/interchange/override decisions, and LIVE -> AWAITING_SCORER_SIGNOFF ->
FINAL lifecycle** as the Grand Final -- `app/service.py`'s
`build_superscore_state()` calls the same `build_matchup_state()` the Grand
Final uses for every entry, then ranks the results. No separate scoring
logic exists for SuperScore.

**Enabling it:** copy `data/superscore_teams.example.json`, fill in real
`canonical_player_id`s per coach, and set `BBBFFL_SUPERSCORE_CONFIG_PATH` to
the copy's path. Leaving the variable unset (the default) disables
SuperScore entirely -- `/superscore` and `/api/*/superscore/*` all 404, and
nothing else about the app changes. A configured-but-malformed file is
logged and disabled at startup rather than crashing the app, so a SuperScore
config mistake can never take down the live Grand Final.

**Isolation:** a coach can have both a Grand Final entry and a SuperScore
entry the same round, potentially sharing some of the same AFL players.
Their scorer decisions (DNP, interchange, overrides) and finalisation state
never leak between the two, because every decision row is scoped by a
`competition_key` in `app/db.py` -- `"grand_final"` for the Grand Final,
`"superscore:<season>:<round>"` for SuperScore (also keeping each
SuperScore round's decisions/result independently addressable for future
historical reporting, without a separate SuperScore table). This holds even
if a Grand Final and a SuperScore entry ever reused the same `team_key`.

**Pages:** `/superscore` (public leaderboard -- two columns of compact
scorecards ranked live by score, click a card for its full lineup, same
data shape as the Grand Final's team view) and `/admin/superscore` (scorer
controls, same DNP/interchange/override/finalise UI as `/admin`, one block
per entry).

**Not implemented yet** (deliberately, per the trial scope): squad-ownership
validation of SuperScore selections, previous-week lineup carry-forward, an
enforced exactly-10-entries count, prize-money split, and a polished
historical results view across rounds -- the `competition_key` scoping
above is what keeps that last one cheap to add later.

## Running locally

```bash
cd bbbffl_app
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # then fill in AFL_API_BASE_URL etc.
edit data/grand_final_teams.json   # replace placeholder canonical_player_id values
set -a && source .env && set +a
.venv/bin/uvicorn app.main:app --reload
```

- Public scoreboard: http://localhost:8000/
- Scorer admin: http://localhost:8000/admin
- Health check: http://localhost:8000/health
- SuperScore (only if `BBBFFL_SUPERSCORE_CONFIG_PATH` is set): public
  leaderboard at http://localhost:8000/superscore, scorer admin at
  http://localhost:8000/admin/superscore

## Running with Docker (home server)

```bash
cd bbbffl_app
docker build -t bbbffl-grand-final .
docker run -d \
  --name bbbffl-grand-final \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  --env-file .env \
  bbbffl-grand-final
```

When using the default local SQLite URL, the `data/` volume makes scorer decisions and the finalised result
survive a container restart. Production uses `BBBFFL_DATABASE_URL` with PostgreSQL. Schema upgrades are described in [`docs/database-migrations.md`](docs/database-migrations.md). Put this alongside the existing `afl-api`
container and expose it later through your reverse proxy of choice; no
changes to `afl-api` are required.

## Tests

```bash
cd bbbffl_app
.venv/bin/pytest
```

Tests cover the scoring engine directly, and interchange assignment / DNP /
override / finalisation behaviour both at the orchestration layer
(`test_service.py`, using a fake afl-api client -- no network) and through
the HTTP API (`test_api.py`).

## afl-api contract status

`app/afl_client.py`'s module docstring documents the **confirmed** live
`/api/v1` response shapes (from live integration testing against a deployed
afl-api instance), including the `{"seasons": [...]}` / `{"rounds": [...]}`
/ `{"matches": [...]}` wrapper keys, `season_id`/`round_id`/`match_id`,
nested `home_team`/`away_team`/`current_team` team objects (matched on
`team_id`, not name), `display_name`, and the nested `stats` object on
`/matches/{id}/player-stats`. `tests/test_afl_client.py` proves the adapter
parses these exact shapes. `afl_client.py` remains the single file to
adjust if a field ever drifts.

## Remaining known assumptions / blockers

1. `data/grand_final_teams.json` currently contains **placeholder**
   `canonical_player_id` values and must be replaced with the real
   coach-declared selection (and real team names) before Grand Final
   weekend. Same for `data/superscore_teams.example.json` if/when
   SuperScore is enabled for a round.
2. No authentication scheme for afl-api was confirmed beyond an optional
   `x-api-key`-style header (`AFL_API_KEY`); adjust `afl_client.py` if the
   real deployment uses something else (e.g. bearer token).
3. The admin interface is gated by a single shared `BBBFFL_ADMIN_TOKEN`
   header, not per-user auth -- adequate for one trusted scorer on a
   private home-server network for one weekend, not a general access
   control model.

## Database migrations

PostgreSQL is the supported production database; SQLite is supported for local
development, tests, replay, and upgrades of existing prototype database files.
Schema evolution is owned by Alembic rather than application startup DDL. See
[`docs/database-migrations.md`](docs/database-migrations.md) for configuration,
validated legacy bootstrap behaviour, operator/developer commands, rollback
policy, and CI expectations.

### Database configuration

Set `BBBFFL_DATABASE_URL` to a SQLAlchemy PostgreSQL URL in production, for
example `postgresql+psycopg://user:password@database/bbbffl`. SQLite remains
the local/test default. See [`docs/database-migrations.md`](docs/database-migrations.md).
