# BBBFFL's `afl-api` `/api/v1` compatibility report

**Issue:** [#18 — Validate and pin afl-api v1 consumer contract](https://github.com/JustPlausible/BBBFFL_Scoring/issues/18)
(roadmap work package **04**, `docs/roadmap/2027-season-roadmap.md`)

**Status:** contract inventoried and pinned by hermetic tests against
source-derived fixtures. **Live validation against the configured
deployment is outstanding** — see [Live validation status](#live-validation-status).

## Sources of truth used, and how they were weighted

Per the issue's descending priority order:

1. **AFL-api source, tests and documentation** (authoritative for intended
   `/api/v1` semantics) — read in full at commit
   `af4bf93f50140fa7d1465a446c63588abc5e376c` on `main` (release `0.7.0`):
   `api/routes_v1.py`, `api/errors_v1.py`, `auth.py`,
   `api_key_capabilities.py`, `afl_json/match_status.py`,
   `afl_json/match_period.py`, `afl_json/player_stats.py`, and every
   `docs/api_v1_*.md` consumer reference plus
   `docs/architecture/workflows/consumer_api_design.md` (the authoritative
   architecture/versioning-policy document).
2. **OpenAPI schema from the deployment** (`/openapi.json`) — **not
   reachable this session** (same host restriction as below); treated as an
   open follow-up, not silently skipped.
3. **Controlled read-only requests to the deployed service** — **not
   reachable this session**. This session's outbound network access is
   blocked by organisation egress policy for `afl-api.thehardinghams.net`
   (confirmed via the local agent proxy's `__agentproxy/status`, which
   recorded `connect_rejected` / gateway `403` for that host on every
   attempt). This is a genuine environment restriction, not a design
   choice — see [Live validation status](#live-validation-status).

Because (2) and (3) were unavailable, this report and its fixtures lean on
(1), which the issue itself ranks highest. Every fixture and every claim
below cites the specific upstream file/module it is derived from.

BBBFFL's own planning documents (`docs/plans/2027-season-model.md`,
`docs/plans/2027-season-decisions.md`, `docs/roadmap/2027-season-roadmap.md`)
are the authority for *what BBBFFL needs*; this report does not restate
their product rules, only the AFL-api contract those rules depend on.

## 1. Validated public contract behaviour

### 1.1 Endpoint inventory

Classification follows the issue's required split: **required now**
(BBBFFL's current `app/afl_client.py` calls it), **committed future
dependency** (named by the 2027 roadmap/season model for a specific later
package), or **potentially useful, non-contractual** (exists upstream, no
documented BBBFFL requirement yet).

| Endpoint | Classification | BBBFFL fields relied upon |
| --- | --- | --- |
| `GET /api/v1` | Potentially useful, non-contractual | Not called by the app. Useful as a cheap connectivity/auth smoke check (used by the diagnostic). |
| `GET /api/v1/seasons` | **Required now** | `seasons[].season_id`, `.is_current`, `.current_round_number`, `.year`. BBBFFL selects the single `is_current: true` entry ([`app/afl_client.py:get_current_season`](../bbbffl_app/app/afl_client.py)). |
| `GET /api/v1/seasons/{season_id}/rounds` | **Required now** | `rounds[].round_id`, `.round_number` (matched against a caller-supplied round number), and `.byes` for package 24's advisory lineup warnings. `null`, an empty list, and a populated list remain distinct evidence states. |
| `GET /api/v1/rounds/{round_id}` | Potentially useful, non-contractual | Not called; BBBFFL currently reaches a round only via the season-scoped list. |
| `GET /api/v1/rounds/{round_id}/matches` | **Required now** | `matches[].match_id`, `.status`, `.home_team{team_id,name}`, `.away_team{team_id,name}`, `.start_time_utc` (consumed by issue #34/package 23's lockout boundary — `app/afl_client.py`'s `Match.start_time_utc`). `.score_home`/`.score_away` are **not yet consumed** — committed future dependency for Round Centre presentation. |
| `GET /api/v1/matches/{match_id}` | Potentially useful, non-contractual | Not called; BBBFFL currently reaches match identity only via the round-scoped list. |
| `GET /api/v1/matches/{match_id}/player-stats` | **Required now** | `players[].canonical_player_id`, `.stats.{goals,behinds,disposals,marks,tackles,hitouts}`. `lifecycle.finality` and `metadata.source_updated_at` are **not yet consumed** — see [1.3](#13-player-stat-finality-and-corrections) and the known gap in [2](#2-bbbffl-assumptions-now-protected-by-tests). |
| `GET /api/v1/players/{canonical_player_id}` | **Required now** | `player.canonical_player_id`, `.display_name`, `.current_team{team_id,name}`. `.identifiers` (AFL/Champion Data crosswalks) is **intentionally not persisted** — see [1.2](#12-identifiers). |
| `GET /api/v1/players/{canonical_player_id}/seasons` | Committed future dependency — package 11 (season player pool) | Per-season `team` scoping; needed so a mid-season/off-season club change never rewrites an earlier season's ownership context. |
| `GET /api/v1/players?search=` | Committed future dependency — package 11 | Name-based identity discovery. **Not a bulk season player-list** — see [Upstream gaps](#3-known-upstream-gaps-and-unresolved-semantics), item 1. |
| `GET /api/v1/injuries` | Committed future dependency — package 27 (DNP evidence) | `injuries[].canonical_player_id`, `.team`, `.injury`, `.estimated_return`, `.current`. Explicitly **not** DNP evidence by itself — an injury listing is a factual AFL report, not a BBBFFL ruling. |
| `GET /api/v1/matches/{match_id}/rosters` | Committed future dependency — packages 23 & 27 | `home_team`/`away_team` `.selections[]` (named, not evidence of participation) and `.context.{ins,outs,late_changes,club_debuts,milestones}`. Confirmed present in current upstream `main` (Issue #219) — this closes part of roadmap gap #3 from `docs/roadmap/2027-season-roadmap.md` section 10. |
| `GET /api/v1/matches/{match_id}/interchanges` and `.../interchanges/events` | Potentially useful, non-contractual | Bench/on-ground CFS evidence that could inform a future Interchange recommendation (package 27). No BBBFFL plan currently names it as required; upstream itself flags one open evidence caveat for `CONCLUDED` matches. Not adopted as a dependency by this issue. |
| `GET /api/v1/matches/{match_id}/commentary` | Potentially useful, non-contractual | No documented BBBFFL feature currently needs commentary text. Not adopted as a dependency by this issue. |

**No roster/lineup endpoint was found missing from current upstream
`main`** — `GET /api/v1/matches/{match_id}/rosters` already exists (Issue
#219). This is a positive finding worth carrying back into the roadmap's
package 04 dependency table.

**No standalone team-list/team-detail endpoint exists.** Team identity is
only available as the `{team_id, name}` projection embedded in match/player
responses. BBBFFL's current model doesn't need more than that; flagged here
so a future full team-list requirement is recognised as a new gap, not an
oversight.

### 1.2 Identifiers

Confirmed authoritative/stable identifiers, and what BBBFFL stores:

| Resource | Authoritative ID | BBBFFL storage |
| --- | --- | --- |
| Season | `season_id` (`afl_seasons.afl_id`) | Stored as `Season.season_id`. |
| Round | `round_id` (`rounds.round_id`) | Stored as `Round.round_id`. |
| Match | `match_id` (`matches.match_id`) — "the same identifier accepted by ... player-stats" (`docs/api_v1_matches.md`) | Stored as `Match.match_id`. |
| Team | `team_id` (`afl_teams.afl_id`) | Stored as `Team.team_id`; BBBFFL matches a rostered player to their live match **by `team_id`, not name** (`app/afl_client.py` module docstring, confirmed by `api/routes_v1.py`'s `MatchTeam` projection). |
| Player | `canonical_player_id` (`canonical_players.id`) — "the primary consumer identity" (`docs/api_v1_players.md`) | Stored as BBBFFL's **sole** AFL player identity throughout (`Player.canonical_player_id`). |
| Player — AFL crosswalk | `identifiers.afl_player_id` | **Not stored.** afl-api remains the authority for this crosswalk; BBBFFL never resolves or re-derives it. |
| Player — Champion Data crosswalk | `identifiers.champion_data_player_id` / `stats[].champion_data_player_id` | **Not stored.** Same reasoning. |

This matches the season model's explicit rule: *"Preserve historical
identity using canonical `afl-api` player IDs, with provisional
reconciliation where necessary"* (`docs/plans/2027-season-model.md`,
design principle 14) — BBBFFL does not add its own identity-inference
layer for AFL players; that authority stays entirely with afl-api.
Protected by
`test_get_player_uses_canonical_player_id_and_does_not_retain_provider_crosswalks`
in `tests/test_afl_contract_v1.py`.

### 1.3 Match lifecycle

**Authoritative field:** `matches.status` (exposed as `Match.status` /
`MatchInfo.status`). Confirmed as the sole source of truth by
`afl_json/match_period.py`'s module docstring in upstream `main`:

> "`afl_json.match_status`/`matches.status` remain the sole source of truth
> for `UPCOMING`/`LIVE`/`POSTGAME`/`CONCLUDED`."

**Vocabulary:** exactly `UPCOMING`, `LIVE`, `POSTGAME`, `CONCLUDED`. This
confirms BBBFFL's existing `app/afl_client.py` assumption is correct, not
merely inferred — it previously cited this same vocabulary without a
verified upstream source; that citation is now backed by the module above.

**`POSTGAME` vs `CONCLUDED` is a real, distinct state, not an alias.**
`POSTGAME` means the siren has sounded but afl-api has not yet declared
statistics final; `CONCLUDED` means it has. BBBFFL must never collapse
these — `app/service.py`'s `PositionState` already keeps them distinct, and
this is protected by
`test_matches_distinguish_all_four_lifecycle_states_within_one_round` and
the existing `test_postgame_is_distinct_from_live_and_completed` in
`tests/test_afl_client.py`.

**Non-authoritative for lifecycle:** `matches/{id}/player-stats`'
`lifecycle.finality` is related but distinct — see 1.3
[player-stat finality](#14-player-stat-finality-and-corrections) below.
`matches/{id}/rosters`' `metadata.match_status_at_observation` is an
independent, separately-named source-status snapshot at roster-observation
time — the API's own doc is explicit this is "distinct from the canonical
`matches.status` lifecycle field." `matches/{id}/interchanges`' `on_bench`
evidence is likewise informational only, per its own field description.
None of these three are authoritative for BBBFFL's match lifecycle;
`matches.status` alone is.

`app/afl_client.py`'s `normalize_match_status` additionally tolerates a set
of legacy/inferred aliases (`FINAL`, `FT`, `COMPLETE`, `IN_PROGRESS`,
`SCHEDULED`, etc.) "kept for backwards compatibility with older fixtures
and any afl-api deployments still emitting them." That tolerance is
retained unchanged by this issue — it is defensive robustness, not a
disagreement with the now-confirmed canonical vocabulary above.

### 1.4 Player-stat finality and corrections

- **Response shape** (`GET /api/v1/matches/{match_id}/player-stats`):
  `match{match_id,round_id,season_id,status,match_provider_id}`,
  `lifecycle{finality}`, `metadata{source_updated_at}`,
  `players[]{champion_data_player_id, canonical_player_id, afl_player_id,
  display_name, side, team_id, stats{goals,behinds,kicks,handballs,
  disposals,marks,tackles,hitouts}}`. BBBFFL scores from
  `goals, behinds, disposals, marks, tackles, hitouts` only; `kicks` and
  `handballs` are present upstream but not part of any BBBFFL formula
  (`docs/plans/2027-season-decisions.md`'s confirmed scoring table) and are
  correctly ignored.
- **Null vs. zero**: confirmed contractually — *"A known zero stat is `0`;
  an unavailable stat is `null`"* (`docs/api_v1_player_stats.md`). See the
  documented adapter gap in [2](#2-bbbffl-assumptions-now-protected-by-tests)
  below: BBBFFL's current parsing does not yet preserve this distinction
  for a resolved player's individual field.
- **Player identity linkage**: a stat row's `canonical_player_id` may be
  `null` (unresolved Champion Data crosswalk). BBBFFL's adapter correctly
  drops such rows rather than inventing an identity — protected by
  `test_get_match_player_stats_drops_rows_with_unresolved_canonical_identity`.
- **Statistics may change during live play**: confirmed —
  `docs/architecture/workflows/consumer_api_design.md` §10.2: *"the normal
  contract represents the latest authoritative fact, not a history of every
  value observed during live polling. If an official value is corrected
  from 11 to 12, consumers receive 12."* BBBFFL has, and needs, no separate
  correction/versioning mechanism of its own for AFL stats — it always
  reads the latest value on each request, matching the architecture
  principle *"BBBFFL must not invent its own AFL-stat correction
  authority"* (issue #18 scope).
- **Final/partial/not-available semantics**: `lifecycle.finality` is one of
  `final`, `partial`, `not_available`, calculated fresh on every request
  from the shared scheduler/storage authority rule, independent of any
  query filter. **BBBFFL does not currently read this field** — see the
  known gap below.
- **Absent players vs. zero-valued statistics**: confirmed — stat rows are
  never synthesised for non-participants; *"Stat endpoints omit
  non-participants. They do not create synthetic rows with zeros or nulls
  for every listed or selected player"* (`consumer_api_design.md` §7.3).
  This matches, and further grounds, the season model's existing rule that
  *"a selected player receiving zero statistics is not by itself proof that
  they did not play"* (`docs/plans/2027-season-model.md`, DNP rulings) —
  the absent-row case and the present-zero-row case are contractually
  different afl-api facts, both distinct from a BBBFFL DNP ruling.

### 1.5 Timing

| Field | Meaning | BBBFFL usage |
| --- | --- | --- |
| `rounds[].start_time` / `.end_time` | Persisted round start/end. | **Not yet consumed** — round mapping (package 17) dependency. |
| `matches[].start_time_utc` | Persisted **UTC** scheduled start, or `null` when unknown. Explicitly *not* a rescheduling/live-update guarantee (`docs/api_v1_matches.md`). | **Consumed** by issue #34/package 23's lockout boundary (`docs/lockouts.md`) as `Match.start_time_utc`. A missing value on an otherwise-`UPCOMING` match is treated as an explicit indeterminate lock state, never guessed. |
| `metadata.source_updated_at` (player-stats) | Newest authoritative source observation among **returned** rows; not request-serve time. | **Not yet consumed.** |

**Timezone semantics, stated explicitly (this was a genuine open question
before this issue):** all afl-api v1 timestamps are UTC
(`consumer_api_design.md` §13: *"UTC is the canonical machine time
representation throughout AFL-api"*), ISO 8601 with an explicit offset or
`Z`. AFL-api deliberately does **not** expose venue-local time or an IANA
timezone in v1 today (`docs/api_v1_matches.md`'s "Field semantics" —
`venue_json` and local `startTime` are explicitly *not* part of the
contract because a timezone is not consistently resolvable). **Consequence
for BBBFFL:** any future BBBFFL local-time presentation (e.g. "lockout at
7:20pm AEST") must convert `start_time_utc` client-side using a
league-configured timezone, not expect afl-api to supply one. This is a
concrete, previously-undocumented constraint for package 23/25 and is
recorded here as a known upstream/design boundary, not a gap to file
against afl-api.

### 1.6 Player membership

`GET /api/v1/players/{id}/seasons` returns one row **per persisted season**,
each independently scoped: *"a later club change never rewrites or is
inferred back onto an earlier season's `team`"* (`docs/api_v1_players.md`).
This directly satisfies the season model's requirement that *"historical
membership must not be inferred from a player's current team"* (issue #18
scope). Protected at the raw-contract level by
`test_player_season_membership_never_rewrites_an_earlier_seasons_team`
(not yet wired into `AflApiClient` — committed future dependency, package
11).

`current_team` on the base player resource is explicitly **current-season
only** and never a fallback from older data — confirmed by
`docs/api_v1_players.md`'s field notes and `api/routes_v1.py`'s
`_current_team` implementation.

### 1.7 Authentication and configuration

- **Header:** `X-Api-Key` (case-insensitive per HTTP), confirmed by
  `auth.py`'s `authenticate_api_key(x_api_key: str | None = Header(None))`
  and every `docs/api_v1_*.md` file. BBBFFL's existing `x-api-key` header
  usage in `app/afl_client.py` is correct — this resolves item 2 of the
  README's "Remaining known assumptions/blockers" list, which is updated
  by this change.
- **Missing/invalid key:** `401` with the **unstructured** body
  `{"detail": "Invalid or missing API Key"}` — deliberately different from
  every other `/api/v1` application-error shape (see below). Confirmed by
  `auth.py` raising a plain FastAPI `HTTPException(401, ...)`.
  A BBBFFL consumer that only understands the structured error shape must
  not silently misinterpret a 401 as some other failure.
- **Structured application errors** (`404`, `403`, `422` on the
  `search`-blank case): `{"error": {"code": "...", "message": "..."}}`,
  confirmed by `api/errors_v1.py` and used consistently across
  `api/routes_v1.py`.
- **Elevated capability:** `advanced-read`, required only for
  `?advanced=true` on the player-stats endpoint. BBBFFL does not need
  advanced/provenance data for any documented feature and does not request
  it — no elevated credential is required for anything in this report's
  scope, satisfying the issue's "Do not require privileged/admin
  credentials."
- **Configuration:** `AFL_API_BASE_URL` and `AFL_API_KEY` already exist as
  exactly-named settings in `app/config.py` (`Settings.afl_api_base_url`,
  `.afl_api_key`), sourced from environment variables, with no hard-coded
  hostname, `/docs` path, or credential anywhere in the repository. The
  base URL is the **service root**; `AflApiClient` composes
  `/api/{contract_version}/...` itself (`app/afl_client.py`'s `_get`),
  matching the issue's requirement. No changes to configuration naming
  were needed — this issue confirms the existing convention already
  satisfies the requirement.
  **Update (issue #38 / roadmap package 06):** `AFL_API_CONTRACT_VERSION`
  (default `v1`, validated against `SUPPORTED_AFL_API_CONTRACT_VERSIONS` in
  `app/config.py`) now makes the expected contract version this pinning
  policy documents an explicit, validated setting rather than an implicit
  literal, and is what `AflApiClient` actually builds every request path
  from. See [`settings.md`](settings.md).

## 2. BBBFFL assumptions now protected by tests

Hermetic, offline tests (`tests/test_afl_contract_v1.py`,
`tests/test_afl_contract_diagnostic.py`) run as part of the normal `pytest`
suite and fail if any of the following drift:

- current-season selection is driven by `is_current`, not list position or
  ordering, and is order-independent;
- historical (non-current) seasons remain reachable through the same
  round-navigation path as the current season;
- `rounds[].byes` distinguishes `null` (unresolved) from `[]` (explicit no
  byes) from a populated list — pinned at the raw-contract level ahead of
  package 24 actually consuming it;
- all four match lifecycle states (`UPCOMING`/`LIVE`/`POSTGAME`/`CONCLUDED`)
  are simultaneously distinguishable within one round, and `POSTGAME` never
  collapses into a neighbour;
- an unresolved-identity player-stat row (`canonical_player_id: null`) is
  dropped, never guessed;
- `canonical_player_id` is BBBFFL's only stored AFL player identity —
  provider crosswalks are never retained;
- season/team player membership is scoped per-season and never rewritten
  by a later club change (raw-contract level; package 11 dependency);
- the structured (`{"error": {...}}`) and unstructured (`{"detail": ...}`)
  error shapes are distinct and both pinned;
- the client tolerates an unknown additive field anywhere in a response
  (compatibility policy's central promise);
- the client fails loudly (raises) rather than silently misinterpreting
  data when a required identifier field is removed or a wrapper key is
  incompatibly renamed.

**Known gap, deliberately pinned rather than silently fixed** (out of this
issue's scope — see [Boundaries](#boundaries)):
`AflApiClient.get_match_player_stats` currently computes each numeric stat
field as `int(row_stats.get(field) or 0)`. For a *resolved* player whose
individual stat field is still `null` (afl-api's genuine "not yet
collected" signal, distinct from a real recorded `0`), this coerces that
`null` into `0`, identically to an actually-recorded zero. This is exactly
the null-vs-zero distinction issue #18 asks to be made explicit rather than
left implicit in code. It is now explicit, both here and in
`test_get_match_player_stats_currently_coerces_null_stat_field_to_zero`,
which pins today's actual behaviour as a regression test. Resolving it
(retaining `None` through to scoring, and deciding what a partial-collection
`None` should mean for a *live* calculated score) belongs to package 05
(resilient AFL client) or package 27 (DNP/finality-aware recommendation),
not this contract-validation issue, per its explicit non-goals.

## 3. Known upstream gaps and unresolved semantics

1. **No bulk season player-list endpoint.** `GET /api/v1/players?search=`
   requires a non-blank name and caps results at 100; `GET
   /api/v1/players/{id}/seasons` is per-player, not per-season. There is no
   `GET /api/v1/seasons/{id}/players`-shaped resource. Package 11 (season
   player pool) needs to enumerate an entire season's eligible player list
   to build the draft pool — this is not currently satisfiable through
   `/api/v1` without either an unbounded/expensive search-by-letter sweep
   (rejected — that is exactly the private-collector-style workaround this
   issue prohibits) or a new upstream endpoint. **This blocks package 11**
   and should be raised upstream before that package starts.
2. **Historical season data presence is unverified.** The contract
   structurally supports historical access (any persisted season/round/
   match/player-stats resource is reachable by ID with no time-window
   restriction), but whether the deployed instance has actually persisted
   full 2026 season data is a live-deployment fact this session could not
   check (see [Live validation status](#live-validation-status)). **This
   blocks packages 08/32** until confirmed.
3. **No standalone team-list/team-detail resource.** Not currently a
   BBBFFL requirement, but worth tracking if a future package needs a full
   AFL club list independent of a match/player projection.
4. **Interchange `on_bench` semantics for `CONCLUDED` matches are an
   openly-flagged upstream caveat** (`api/routes_v1.py`'s
   `InterchangeStatus.on_bench` field description): confirmed for `LIVE`
   and `POSTGAME`, not yet independently verified for `CONCLUDED`. Relevant
   only if/when package 27 adopts the interchange-evidence endpoints — not
   currently a BBBFFL dependency, so not a blocker today.
5. **No IANA timezone / venue-local time in v1** (see
   [1.5 Timing](#15-timing)) — not a defect, but a real design boundary
   package 23/25 must plan around (client-side timezone conversion).

### Proposed upstream follow-up (not filed; for maintainer review)

> **Title:** Expose a bulk season-scoped canonical player list
>
> **Problem:** `/api/v1` has no endpoint returning every canonical player
> associated with a given season (only per-player `.../seasons` lookup and
> a capped, name-required search). A consumer building a season-long player
> pool (e.g. a fantasy draft) currently has no practical way to enumerate
> the full eligible player set without already knowing every player's name
> or ID.
>
> **Suggested shape:** `GET /api/v1/seasons/{season_id}/players`, reusing
> the existing `CanonicalPlayer` projection (`canonical_player_id`,
> `display_name`, season-scoped `team`, `identifiers`), backed by
> `competition_season_players` for that `season_id` the same way
> `.../players/{id}/seasons` already reads that table per-player. Simple
> `limit`/`offset` pagination would be consistent with the consumer API
> design doc's stated approach to "potentially large detail/history
> collections."
>
> **Why it matters:** this is the one remaining structural gap blocking a
> documented downstream consumer requirement (BBBFFL roadmap package 11,
> season player pool) from being satisfiable through the public consumer
> contract, without resorting to a private/internal workaround.

## 4. Future work belonging to package 05 or package 08

**Package 05 is now done** — see
[`docs/afl-client-resilience.md`](afl-client-resilience.md) (issue #37) for
the explicit timeout policy, bounded retry/backoff, request correlation,
and response cache with freshness/provenance metadata built around
`AflApiClient`. The remaining items below were explicitly **not** done as
part of this issue (#18) — listed so nobody mistakes their absence for an
oversight:

- consuming `lifecycle.finality`, `metadata.source_updated_at`, roster,
  injury, or interchange data in application/scoring code (packages
  11/23/27, as annotated per-endpoint above);
- fixing the null-vs-zero coercion gap in §2 (package 27);
- ~~the full deterministic AFL evidence fixture corpus for the 2026
  replay (package 08)~~ — delivered by issue #40; see
  [`afl-evidence-fixtures.md`](afl-evidence-fixtures.md). This issue's own
  fixtures (`tests/fixtures/afl_api_v1/`) remain the smaller,
  contract-pinning set, separate from that corpus;
- season player pool, lockout engine, replay harness, or scoring
  generalisation (explicitly out of scope for issue #18).

## Compatibility and pinning policy

1. BBBFFL supports the **public, versioned `afl-api` `/api/v1` consumer
   contract** — never Champion Data/CFS directly, never afl-api's internal
   database/schema, never scraping, never the legacy unversioned
   `/api/...` routes (which afl-api's own architecture document states are
   "pre-v1 legacy behaviour, not a permanently supported parallel API").
2. **Deployment hostname and API credentials are configuration, not
   compatibility identifiers.** `AFL_API_BASE_URL` and `AFL_API_KEY` may
   change at any time without representing a contract change; BBBFFL code
   must never hard-code either.
3. **Additive compatible v1 changes must not break BBBFFL.** New optional
   fields, new resources, and new filters may appear at any time — pinned
   by `test_client_tolerates_unknown_additive_fields`. This mirrors
   afl-api's own stated policy: *"additive optional fields, filters, and
   new resources may be introduced [within v1]"*
   (`consumer_api_design.md` §15).
4. **Removal, renaming, type changes, or semantic changes to a field
   BBBFFL actually depends on are incompatible.** Pinned by
   `test_client_fails_loudly_when_a_required_identifier_field_is_removed`
   and `test_client_fails_loudly_when_matches_wrapper_key_is_renamed_incompatibly`.
5. **Identifier, lifecycle, nullability, timing, and stat-finality
   semantics are potentially breaking even where the JSON shape stays
   technically valid.** For example, afl-api renaming the lifecycle
   vocabulary, or changing what `null` means for a stat field, would not
   necessarily fail JSON-schema validation but would be a real BBBFFL
   contract break. This report exists specifically to make those semantics
   explicit (§1.3–§1.6) rather than leaving them implicit in scattered
   client code.
6. **An incompatible deployment must fail explicitly, not silently
   reinterpret data.** `AflApiClient` already raises `AflApiError` on HTTP
   failure and lets a missing required field raise a plain `KeyError`/
   `TypeError` rather than substituting a default; this issue's tests pin
   that behaviour rather than relaxing it.
7. **BBBFFL does not pin a specific afl-api patch release.** Fixtures and
   tests target the documented v1 contract's stable semantics, which
   afl-api's own versioning policy commits to keeping additive within v1
   (`consumer_api_design.md` §15). A deployment on any `0.7.x`-or-later
   release that still satisfies this contract is supported without a
   BBBFFL code change.
8. **Migration to a future `/api/v2` must be an explicit BBBFFL change** —
   never an implicit reinterpretation of `/api/v1` responses, and never a
   silent fallback inside `AflApiClient`.

## Live validation status

**Not completed in this session.** This session's outbound network access
is routed through a policy-enforcing egress proxy
(`/root/.ccr/README.md`), and `afl-api.thehardinghams.net` is blocked by
that policy — every attempted connection (via `httpx`, and via the opt-in
diagnostic itself) returned a proxy-level `403` / `connect_rejected`,
confirmed through the proxy's own `__agentproxy/status` diagnostic
endpoint. Per that proxy's documented guidance, this is an organisation
policy denial to report, not a failure to route around.

**What this means concretely:**

- The fixtures in `bbbffl_app/tests/fixtures/afl_api_v1/` are
  source-derived (see that directory's `PROVENANCE.md`), not live-captured.
  They are still the higher-priority source per the issue's own ranking,
  but a live-capture cross-check remains outstanding.
- `/openapi.json` could not be compared against BBBFFL's expectations.
- The opt-in diagnostic (`bbbffl_app/scripts/afl_contract_diagnostic.py`)
  was run against the configured `AFL_API_BASE_URL`/`AFL_API_KEY` and
  correctly reported every required check as a clean, credential-free
  `FAIL` with a network-error detail (proxy `403`) rather than crashing —
  proving the diagnostic itself is sound, but it did **not** produce a
  positive live validation of the deployment.
- Gap #2 in [§3](#3-known-upstream-gaps-and-unresolved-semantics)
  (historical 2026 data presence) remains genuinely unverified as a result.

**Required follow-up before packages 08/32 begin:** run
`python -m scripts.afl_contract_diagnostic` (see below) from an environment
with network access to the configured `afl-api` deployment, and reconcile
any discrepancy between its findings and this report's source-derived
fixtures before trusting them for replay evidence.

## Running the opt-in live integration diagnostic

Never part of hermetic CI or plain `pytest`. Read-only; makes no mutating
requests.

```bash
cd bbbffl_app
export AFL_API_BASE_URL=https://afl-api.example.net   # service root, no /api/v1 suffix
export AFL_API_KEY=...                                 # a real consumer key -- never committed/logged
python -m scripts.afl_contract_diagnostic
```

Exit code `0` only if every **required** check passes. Optional checks
(committed-future-dependency endpoints BBBFFL doesn't consume yet, and the
best-effort `/openapi.json` compatibility check) are always reported but
never affect the exit code — normal BBBFFL operation requires only
`/api/v1`, never Swagger/OpenAPI availability. The API key is read only via
`app.config.get_settings()` and is never printed, logged, or included in
any output.

## OpenAPI usage

`/openapi.json` is validation evidence only, used by the diagnostic's
optional compatibility check. No part of BBBFFL's normal runtime depends on
it being reachable — the actual integration boundary is `/api/v1` itself.
