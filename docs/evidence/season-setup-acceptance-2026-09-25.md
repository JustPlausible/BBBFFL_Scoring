# Season setup clean-database acceptance run (issue #237)

**Date:** 25 September 2026.
**Scope:** the issue #237 [Season setup](../season-setup.md) workflow, run
against a clean, disposable, production-like environment.

**Classification:** a disposable acceptance run. This is **not** a staging
rehearsal, and it did not use the real production afl-api deployment. The
AFL data was synthetic and served by a stand-in afl-api HTTP server. See
"What this does not prove".

## Environment

- The application ran as a real `uvicorn app.main:app` process with:
  - `BBBFFL_ENVIRONMENT=production`, so every production settings check
    applied (PostgreSQL URL, admin token, session secret, public base URL,
    live AFL mode);
  - a freshly created, empty PostgreSQL 16 database, migrated to head at
    startup.
- `AFL_API_BASE_URL` pointed at a stand-in `/api/v1` server serving the
  public contract shapes: seasons, rounds with byes, round matches, and the
  paginated season player list.
  - The data was one synthetic 2027 AFL season with 540 players (three pages)
    and an Opening Round for 8 clubs, with byes in AFL Rounds 2-4.
  - The app used its normal live client stack:
    `AflApiClient` → `ResilientAflClient`.
- The browser was Chromium, driven by Playwright, using real page scripts,
  real session cookies and double-submit CSRF.

## Steps and results

**A. Season identity** (the existing Season Centre and admin APIs, with the
admin token):

- The database contained no season.
- Created season 2027, ten coaches and ten teams.
- Provisioned a Scorer credential and a Scorer role grant. Credential
  provisioning is the separate readiness item 2.

**B. Preseason** (the browser, signed in as the Scorer):

1. Login, then `/account` → **Open Scorer dashboard** (role switch).
2. On the Scorer dashboard, the **Season setup** card link led to the setup
   page. Next safe action: *Player pool (live afl-api)*.
3. **Load AFL seasons**: the page preselected
   *2027 Toyota AFL Premiership Season (matches this season)*.
4. **Populate from live afl-api**: 540 added, 0 updated, 0 unchanged
   (pool: 540).
5. **Create rules version, ordinary stream and Rounds 1-20**: initialized.
6. **Check the live AFL fixture for an Opening Round**: 8 clubs listed, each
   with its derived compensating bye (Rounds 2/3/4) and recommended BBBFFL
   target. **Accept these rules**: accepted.
7. **Squad limit** 22: saved.
8. **Draft order**: chose positions 1-10 by team name and accepted. Result:
   *Complete — Accepted: 0 of 220 picks made*, with links to the draft board
   and preseason pages. Next safe action: *Fixture-number draw*.
9. Finals step: *Blocked — 20 regular-season round(s) are not final yet*.
   A direct premature Finals request returned **409**: "every regular-season
   round through 20 must be final…". Nothing was created.
10. Repeated ordinary initialization returned **200**, `created: false`.
11. The first-pick Coach signed in, opened `/account/preseason-draft/{season}`,
    and made Pick 1: 200, completed picks = 1.

**C. Stand-in for the weekly season** (the normal services, no SQL):

- Froze the fixture draw.
- Accepted each round's AFL mapping (validated through the live-mode client).
- Took all 20 ordinary rounds open → live → review → published (atomic
  five-result publication), with strictly ordered synthetic scores, so the
  ladder is untied.

**D. Finals and SuperScore** (the browser, as the Scorer):

1. Scorer dashboard next action: `initialize_finals` — *Home-and-away season
   complete — initialize Finals*, linking to Season setup.
2. The Finals step showed the seed preview from the final ladder (Teams 1-5
   qualify, 6-10 eliminated). **Create Finals stream and bracket** gave:
   *Bracket created (seeded from the ladder)*.
3. **Create SuperScore stream and SS1–SS4** gave: *SuperScore stream with
   SS1-SS4 created*. Next safe action: *No setup action is currently
   outstanding*.
4. Repeated Finals initialization returned **200**, `created: false`.
5. The Round preflight index now lists *Finals Week 1, Finals Week 2,
   Preliminary Final, Grand Final*. The Scorer dashboard moved on to
   *Complete Finals Week 1 preflight*.

**Audit attribution.** Every setup action was recorded against
*Acceptance Scorer* (`actor_role=scorer`) with the reason entered on the page:

- `player_pool.season.refreshed`
- `season.rules_version.created`
- `season.ordinary_competition.initialized`
- `opening_round.rule.accepted`
- `ownership.squad_limit.configured`
- `draft.order.accepted`
- `finals.stream.created`
- `finals.bracket.created`
- `superscore.stream.created`
- `superscore.rounds.initialized`

**2026 bootstrap guard.** `python -m scripts.bootstrap_2026_first_half` was
run with `BBBFFL_ENVIRONMENT=production` against the same database. It exited
1 with "Refusing to run the 2026 first-half replay bootstrap while
BBBFFL_ENVIRONMENT=production". The audit row count was unchanged
(257 → 257).

No direct SQL, UUID entry, or 2026 replay script was used for any
initialization step. UUIDs appear only inside the automation script's own
plumbing, never typed by an operator.

## What this does not prove

- **The real production afl-api deployment.** Readiness item 11 remains
  outstanding. The live season-player endpoint's real behaviour (page size,
  team resolution for every player) still has to be confirmed against the
  deployed service.
- **A non-technical operator, or a real hosting environment.** Readiness
  item 6 is not covered.
- **Weekly operation.** Phase C used the normal services directly, not the
  browser preflight/lockout/review pages. Those are covered by their own
  evidence.
