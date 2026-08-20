# BBBFFL 2027 consumer prototype — Grand Final live scoring

## Purpose

Build the first fresh prototype of the 2027 BBBFFL consumer application in `JustPlausible/BBBFFL_Scoring`.

This is a new implementation. **Do not refactor or extend the legacy Google Apps Script projects as the application architecture.** They remain historical/reference implementations. The recovered 2025 implementation and its forensic review on the audit branch should be used to recover legacy behaviour where required.

The immediate goal is a small working prototype suitable for live testing during the **2026 BBBFFL Grand Final weekend**.

## Branch and repository approach

Implementation should be performed on a new feature branch from `main`, for example:

`feature/2027-grand-final-prototype`

Keep the existing root layout and legacy Apps Script directories intact during this prototype. In particular, **do not relocate the existing GAS projects into a `legacy/` directory as part of this work**. Avoid unrelated repository cleanup so the prototype diff remains focused and reviewable.

Before implementation, inspect the repository and the historical forensic review, report the proposed new application/file structure, and identify assumptions or blockers. Then implement the smallest coherent vertical slice.

## Architecture

BBBFFL is a consumer of the separate `afl-api` service. AFL data remains authoritative in `afl-api`; BBBFFL must not reproduce AFL collection or maintain a second authoritative copy of AFL statistics.

Prefer a small Python/FastAPI application suitable for running in its own Docker container alongside `afl-api` on a home server.

Create a clean new application area without unnecessarily reorganising the legacy Apps Script directories.

## AFL API workflow

Use the versioned consumer API only.

Current useful endpoints include:

```text
GET /api/v1/seasons
GET /api/v1/seasons/{season_id}/rounds
GET /api/v1/rounds/{round_id}/matches
GET /api/v1/players/{canonical_player_id}
GET /api/v1/players?search={name}
GET /api/v1/matches/{match_id}/player-stats
```

`GET /api/v1/seasons` identifies the current season using `is_current=true` and supplies `current_round_number`.

Player lookup/search returns canonical identity, current AFL team, and provider crosswalks. **BBBFFL should use `canonical_player_id` as its stored AFL player identity.**

The intended runtime flow is approximately:

```text
current season
  -> current round number
  -> round ID
  -> round matches
  -> selected canonical player
  -> current team
  -> relevant AFL match
  -> match player stats
  -> BBBFFL score
```

Resolve selected player identity/team information once when appropriate rather than on every live refresh. Group selected players by AFL match and fetch each unique match's player-stat collection once per refresh rather than making requests per player.

For this live-current-round prototype, `current_team` is acceptable for match discovery. Do not generalise that into historical team authority without a future season-aware design.

## Grand Final team configuration

For this prototype, do not rebuild Google Forms or Sheets.

Define the two BBBFFL Grand Final teams using a simple checked-in JSON configuration containing:

- BBBFFL team name;
- BBBFFL position;
- canonical AFL player ID.

Positions are:

- Forward 1
- Forward 2
- Forward 3
- Midfield 1
- Midfield 2
- Midfield 3
- Ruck
- Tackler
- Interchange

The team configuration represents the **coach-declared selection** and must remain separate from later scorer decisions.

## Scoring

Implement one canonical BBBFFL scoring engine.

Recover the existing scoring rules from the legacy implementation/audit rather than inventing new rules. The established rules are not being changed for this prototype.

AFL statistics obtained from `afl-api` are authoritative and must not be modified by BBBFFL.

The same scoring implementation must produce both live and final scores.

## Interchange / loophole behaviour

Interchange is not intrinsically tied to one scoring position.

The interchange player may replace **any one starting position**, subject to a scorer decision.

This supports BBBFFL's existing loophole behaviour: for example, a Thursday-night interchange player may later be promoted into a deliberately vacant position once subsequent AFL teams are known.

A DNP starter may similarly make a position eligible for interchange replacement. More than one starting position could theoretically be vacant or DNP, so the scorer remains responsible for choosing the effective position.

The system may **recommend** an appropriate interchange assignment, but must never silently make the official scorer ruling.

Scorer assignments remain provisional and editable throughout the round until explicit finalisation.

## Scorer/admin controls

Provide a deliberately small admin/scorer interface allowing the scorer to:

- mark/unmark a player as DNP;
- assign Interchange to any starting position;
- remove/change an interchange assignment;
- enter/remove a direct BBBFFL score override;
- provide a short reason for a score override;
- see recommendations for unresolved vacant/DNP positions;
- explicitly finalise the Grand Final.

All scorer actions should remain reversible before finalisation.

Store scorer decisions separately from the declared team configuration. SQLite is appropriate for this prototype and should survive container restarts.

A manual override changes the resulting BBBFFL point score only. It does **not** alter AFL source statistics:

```text
AFL stats
  -> calculated BBBFFL score
  -> optional scorer override
  -> effective BBBFFL score
```

The admin view should make calculated versus overridden/effective score apparent. The public page normally needs only the effective score.

## AFL statistics and corrections

AFL/Champion Data statistics are the source of truth. BBBFFL does not manually correct individual goals, disposals, tackles, hitouts, etc.

Upstream statistics may change shortly after a match through normal statistical review. Continue consuming authoritative `afl-api` values until scorer sign-off so upstream corrections can naturally flow into BBBFFL scoring.

## Matchup lifecycle

Do not automatically declare the BBBFFL Grand Final final simply because the final AFL match finishes.

Use approximately:

```text
LIVE
  -> all relevant AFL matches final
  -> AWAITING_SCORER_SIGNOFF
  -> explicit scorer finalisation
  -> FINAL
```

Scorer decisions remain editable until explicit finalisation.

## Public Grand Final page

Provide a simple responsive/mobile-friendly web page showing the two BBBFFL teams and a prominent running total for each.

Each team should display players in BBBFFL position order with approximately:

```text
Position | Player | AFL club | Match state | BBBFFL score
```

AFL club may be presented using a short form suitable for a compact table.

Visually distinguish, without overcomplicating the first prototype:

- yet to play;
- live;
- completed;
- DNP;
- unresolved interchange/vacancy.

The public page should show **confirmed scorer decisions in the official total**. Recommendations must not silently alter the official score. A potential/recommended state may be shown separately if useful.

Simple polling is sufficient for automatic refresh. Approximately 20–30 seconds is appropriate. Do not introduce WebSockets or unnecessary realtime infrastructure for this prototype.

A modest Grand Final summary may show useful derived context such as current leader/margin and counts of players live, completed, or yet to play, provided this remains a small addition rather than expanding the project scope.

## Persistence and recovery

For this prototype:

- declared teams -> JSON;
- scorer decisions -> SQLite;
- AFL statistics -> obtained from `afl-api`, not persisted as a second authoritative dataset;
- current calculated state should be reconstructable after application/container restart;
- scorer decisions must survive restart;
- finalisation should preserve enough result/scorer state for historical inspection.

Keep coach selection, AFL facts, scorer rulings, and scoring calculation conceptually separate:

```text
coach selection + AFL facts + scorer decisions -> official BBBFFL score
```

## Operational requirements

Include:

- Docker support;
- environment configuration for AFL-api base URL/API authentication;
- no committed credentials;
- useful application/structured logging;
- a health endpoint;
- straightforward README instructions for local/home-server startup;
- tests for the scoring engine;
- tests for interchange assignment, DNP and direct score override behaviour.

The application should be suitable for running as its own container alongside the existing `afl-api` deployment on the home server and later being exposed through a separately configured domain/reverse proxy.

## Explicitly out of scope

Do **not** implement yet:

- Google Forms integration;
- Google Sheets integration;
- coach authentication/submission UI;
- full-season fixtures;
- ladder;
- SuperScore;
- historical migration;
- sophisticated projections;
- AI commentary;
- duplicated AFL data collection;
- broad refactoring/reorganisation of the legacy Apps Script implementation.

Google Forms/Sheets remain potentially useful future coach-facing and administrative interfaces; excluding them from this prototype is not a decision to retire them.

## Desired first milestone

The deliberately narrow vertical slice is:

**two manually configured BBBFFL Grand Final teams -> live `afl-api` data -> canonical BBBFFL scoring -> individual player/running team scores -> scorer-controlled interchange/DNP handling -> final scorer sign-off.**

This weekend's prototype should also serve as a genuine external consumer integration test of the `/api/v1` AFL API under live-match conditions.

## Implementation-agent instruction

Before writing code:

1. inspect `main` and the relevant historical/audit documentation;
2. propose the minimal new application/file structure;
3. identify assumptions, API-contract uncertainties, or blockers;
4. confirm how the existing BBBFFL scoring rules will be recovered and tested.

Then implement the smallest coherent vertical slice described above.

Do not expand scope simply because additional legacy functionality exists.