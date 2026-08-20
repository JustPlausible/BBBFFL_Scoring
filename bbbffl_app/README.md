# BBBFFL Grand Final live scoring (2027 prototype)

A small FastAPI service implementing the narrow vertical slice from
`docs/plans/2027-grand-final-prototype-brief.md`:

> two manually configured BBBFFL Grand Final teams -> live `afl-api` v1 data
> -> canonical BBBFFL scoring -> individual player/running team scores ->
> scorer-controlled interchange/DNP handling -> final scorer sign-off.

This is a new, self-contained application area. It does not touch, reuse,
or reorganise the legacy Google Apps Script projects elsewhere in this
repository (`AFL_Stats/`, `BBBFFL_Results/`, `BBBFFL_Weekly_Teams/`), which
remain historical/reference implementations.

## What this is not

Per the brief, this prototype deliberately excludes: Google Forms/Sheets
integration, coach authentication, full-season fixtures, the ladder,
SuperScore, historical migration, projections, and AI commentary. AFL data
collection and identity resolution live entirely in `afl-api`; this service
only consumes `/api/v1`.

## Architecture

```
bbbffl_app/
  app/
    afl_client.py   # thin adapter over afl-api /api/v1 (the one seam to
                     # touch if a response field name differs from what's
                     # assumed here -- see the module docstring)
    scoring.py       # canonical BBBFFL scoring formulas (pure functions)
    teams.py         # loads the coach-declared team JSON (read-only)
    db.py            # SQLite store for scorer decisions (DNP, interchange,
                      # overrides, finalisation) -- separate from teams.py
    service.py        # orchestration: AFL data + team config + scorer
                       # decisions -> the official scoreboard
    routes/            # health, public (read-only), admin (scorer-gated)
    templates/           # server-rendered public + admin pages, JS polling
  data/
    grand_final_teams.json   # coach-declared selection (edit before use!)
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
(`BBBFFL_Results/fetchBBBFFLResults.js` and
`BBBFFL_Results/generateLiveBBBFFLMatches.js` on the
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
an *active* (non-DNP, non-vacant) roster position depends on is complete.
Moving to `FINAL` always requires an explicit `POST /api/admin/finalize`
call -- the system never finalises itself.

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

The `data/` volume is what makes scorer decisions and the finalised result
survive a container restart. Put this alongside the existing `afl-api`
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

## Known assumptions / blockers to confirm against the real afl-api

These are documented in detail in `app/afl_client.py`'s module docstring;
summarised here:

1. Exact JSON field names on `/seasons`, `/rounds`, `/matches`, `/players`,
   `/player-stats` are inferred from the brief's prose, not a live schema.
   `afl_client.py` is the single file to adjust if they differ.
2. Match completion is assumed to be a `status` string
   (`normalize_match_status` treats `FINAL`/`FT`/`COMPLETE`/`COMPLETED` as
   final, `LIVE`/`IN_PROGRESS` as live, anything else as yet-to-play,
   case-insensitively). Extend that function if the real API uses other
   values.
3. `data/grand_final_teams.json` currently contains **placeholder**
   `canonical_player_id` values and must be replaced with the real
   coach-declared selection (and real team names) before Grand Final
   weekend.
4. No authentication scheme for afl-api was confirmed beyond an optional
   `x-api-key`-style header (`AFL_API_KEY`); adjust `afl_client.py` if the
   real deployment uses something else (e.g. bearer token).
5. The admin interface is gated by a single shared `BBBFFL_ADMIN_TOKEN`
   header, not per-user auth -- adequate for one trusted scorer on a
   private home-server network for one weekend, not a general access
   control model.
