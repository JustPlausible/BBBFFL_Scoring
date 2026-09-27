# BBBFFL's `afl-api` `/api/v1` compatibility report

**Issue:** [#18 — Validate and pin afl-api v1 consumer contract](https://github.com/JustPlausible/BBBFFL_Scoring/issues/18)
(roadmap work package **04**, `docs/roadmap/2027-season-roadmap.md`);
live-deployment validation completed by
[#244 — Validate live afl-api deployment and application contract before v0.1](https://github.com/JustPlausible/BBBFFL_Scoring/issues/244).

**Status:** contract inventoried and pinned by hermetic tests against
source-derived fixtures, **and now positively validated against the live
2027 `afl-api` deployment** (2026-09-27) — see
[Live validation status](#live-validation-status).

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

**Update (issue #244, 2026-09-27):** (2) and (3) were both completed in a
later session whose network path could reach `afl-api.thehardinghams.net`
— see [Live validation status](#live-validation-status) for the results.
This section is left as the historical record of what the original #18
report could and could not do.

Because (2) and (3) were unavailable at the time, this report and its fixtures lean on
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
| `GET /api/v1/seasons/{season_id}/players` | **Required now** (live season player-pool population, issue #237 -- `AflApiClient.get_season_players`) and for the supported 2026 historical replay | Complete season-scoped canonical player pool backed by `competition_season_players`, ordered `canonical_player_id` ascending and paginated `limit`/`offset` (max/default page size 250) — the replay exporter follows every page automatically. Each row carries `canonical_player_id`, `display_name`, `given_name`, `family_name`, the requested season's `team` (not `current_team`; may be `null` if membership is unresolved, in which case BBBFFL fails acquisition rather than guessing) and provider `identifiers`. `given_name`/`family_name` (afl-api commit `d21d15a`, issue #248) are authoritative but nullable structured-name facts, preserved by BBBFFL alongside `display_name` and never derived by splitting it. There is **no `eligible` field** — BBBFFL's replay output marks every acquired member eligible as its own policy, never as an afl-api fact. The replay exporter must not infer this pool from match participants. The live client applies the identical fail-closed paging/row validation (`AflSeasonPlayersContractError`), is never served from a stale cache (`season_players` stale TTL 0), and the Season setup page refuses anything but fresh evidence. |
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

1. **Bulk season player-list dependency is now consumed by replay.** The
   supported first-half exporter requires `GET
   /api/v1/seasons/{season_id}/players?limit=250&offset=...`, following every
   page (a page shorter than the requested limit, including an empty page,
   terminates the collection); it deliberately fails closed if the deployed
   consumer API does not provide the complete season-scoped pool, returns
   mismatched pagination progress, or repeats a canonical player across
   pages. This endpoint supersedes the earlier gap recorded below and
   prevents injured, suspended, or pre-debut eligible players being omitted
   merely because they have no first-half stat row.
2. **Historical season data presence — now fully confirmed (issue #244,
   2026-09-27).** The contract structurally supports historical access
   (any persisted season/round/match/player-stats resource is reachable by
   ID with no time-window restriction), and the deployed instance has
   confirmed persisted this completely: season 85 (2026) carries all 30
   rounds and all 218 matches, every one reporting `CONCLUDED` — see
   [Live validation status](#live-validation-status). Player-stats
   completeness was checked twice, ~75 minutes apart, and genuinely
   changed state between the two checks:
   - **First pass (~11:47 UTC):** complete and contract-compliant for the
     specific match sampled (Round 1's Carlton v Richmond, `match_id`
     8045), but **not** for every match — three matches (the two
     Preliminary Finals, `match_id` 9026/9027, and the Grand Final,
     `match_id` 9028) reported `lifecycle.finality="not_available"` with
     zero player rows. This correctly blocked packages 08/32 at the time.
   - **Second pass (~12:51 UTC), after the operator reported the upstream
     provider had backfilled those three matches:** all three now report
     `lifecycle.finality="final"` with 46 player rows each. A full
     season-wide sweep of all 218 matches in season 85 (every
     `GET /api/v1/matches/{id}/player-stats`, not a single sample) found
     **zero** incomplete matches — every match reports `finality="final"`
     with at least one player row.

   **This no longer blocks packages 08/32.** The upstream data gap was
   real, genuinely time-bound, and is now closed by the provider's own
   backfill, not by BBBFFL relaxing what "complete" means — see
   [Live validation status](#live-validation-status) for the season-wide
   sweep methodology.
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

### Historical proposed upstream follow-up (now satisfied)

> **Title:** Expose a bulk season-scoped canonical player list
>
> **Problem:** `/api/v1` has no endpoint returning every canonical player
> associated with a given season (only per-player `.../seasons` lookup and
> a capped, name-required search). A consumer building a season-long player
> pool (e.g. a fantasy draft) currently has no practical way to enumerate
> the full eligible player set without already knowing every player's name
> or ID.
>
> **Delivered shape (AFL-api issue #247 / PR #248):** `GET
> /api/v1/seasons/{season_id}/players?limit=250&offset=0`, reusing the
> existing `CanonicalPlayer` projection (`canonical_player_id`,
> `display_name`, season-scoped `team`, `identifiers`), backed by
> `competition_season_players` for that `season_id` the same way
> `.../players/{id}/seasons` already reads that table per-player. Ordering
> is `canonical_player_id` ascending; pagination is `limit`/`offset` with a
> max/default page size of 250. `team` is the requested season's membership
> team (never `current_team`), and may be `null` when unresolved — a valid
> upstream representation that BBBFFL's replay acquisition treats as fatal
> rather than guessing. The endpoint does not depend on StatsPro, Home &
> Away summaries, match appearances, rosters, or the legacy players table.
>
> **Why it matters:** this was the one remaining structural gap blocking a
> documented downstream consumer requirement (BBBFFL roadmap package 11,
> season player pool) from being satisfiable through the public consumer
> contract, without resorting to a private/internal workaround. It is now
> satisfied and consumed by `app/replay_acquisition.py`.

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

**Completed 2026-09-27, issue #244.** An earlier session (issue #18)
recorded this as blocked: its outbound network access was routed through a
policy-enforcing egress proxy that rejected every connection to
`afl-api.thehardinghams.net` with a proxy-level `403`/`connect_rejected`.
That restriction did not apply to the session that ran this validation —
its egress proxy has a provisioned, credential-injecting allow rule for
`afl-api.thehardinghams.net` (the intended BBBFFL 2027 `afl-api`
deployment), so outbound requests to that host are transport-level
authenticated automatically without this session ever holding or seeing an
`AFL_API_KEY` value. `AFL_API_BASE_URL=https://afl-api.thehardinghams.net`
and a placeholder, non-secret `AFL_API_KEY` (required only to satisfy
`get_settings()`'s "a key must be configured" check; never the credential
actually used on the wire) were set, and the diagnostic was run unchanged
from `bbbffl_app`:

```bash
AFL_API_BASE_URL=https://afl-api.thehardinghams.net AFL_API_KEY=*** \
  python -m scripts.afl_contract_diagnostic
```

No secret was printed, logged, retained, or committed at any point; the
value above is a placeholder, not the credential actually used (see
"Authentication/configuration — now positively confirmed" below).

### Endpoint/contract coverage — confirmed positive

Every endpoint this document classifies **required now** was exercised
against real data and returned a contract-compatible shape:

| Endpoint | Representative evidence retrieved |
| --- | --- |
| `GET /api/v1` | `{"name": "AFL-api", "version": "0.7.0", "documentation": "/docs"}` |
| `GET /api/v1/seasons` | 15 persisted seasons (2012–2026); confirms multi-year historical access, not only the most recent season |
| `GET /api/v1/seasons/{season_id}/rounds` | Season 85 (2026): 30 rounds, Opening Round through Grand Final, `byes` correctly array-or-null |
| `GET /api/v1/seasons/{season_id}/players` | Season 85: complete pool followed to exhaustion at the production page size (812 players across 4 pages of `SEASON_PLAYERS_PAGE_LIMIT`=250, `limit`/`offset` echoed exactly on every page, no repeated `canonical_player_id` across pages), every row's *values* -- not just key presence -- validated against `AflApiClient.get_season_players`'s own rules (positive `canonical_player_id`, non-blank `display_name`, optional non-blank `given_name`/`family_name`, a resolved season-scoped `team`) |
| `GET /api/v1/rounds/{round_id}/matches` | Every one of season 85's 218 matches, across all 30 rounds, reports `status="CONCLUDED"` (the 2026 season has fully finished) |
| `GET /api/v1/matches/{match_id}` | Round 1 and Grand Final match detail, correct `home_team`/`away_team`/`score_home`/`score_away` shape |
| `GET /api/v1/matches/{match_id}/player-stats` | Round 1 match (Carlton v Richmond): `lifecycle.finality="final"`, 46 player rows, every BBBFFL-scored field (`goals, behinds, disposals, marks, tackles, hitouts`) present on every row. **Season-wide sweep** (second validation pass, ~12:51 UTC): all 218 matches in season 85 checked individually, all report `lifecycle.finality="final"` with player rows present -- zero incomplete matches |
| `GET /api/v1/players/{canonical_player_id}` | Resolved a real `canonical_player_id` from the match player-stats above and confirmed `display_name`/`current_team`/`identifiers` |
| `GET /api/v1/players?search=` | Non-empty result for a real surname |
| `GET /api/v1/injuries` | 245 current records, correct shape |
| `GET /api/v1/matches/{match_id}/rosters` | Correct `home_team`/`away_team` shape |
| 404/422 structured error shapes | Confirmed: `player_not_found` / `search_required`, exactly as documented in [§1.7](#17-authentication-and-configuration) |

This closes gap #2 in
[§3](#3-known-upstream-gaps-and-unresolved-semantics) (historical 2026 data
presence was genuinely unverified before this session): season 85 (2026)'s
round/match structure is confirmed fully present (30 rounds, 218 matches,
all `CONCLUDED`), and player-stats are now confirmed complete and
contract-compliant **season-wide, not just for one sampled match**.

This was not true on the first validation pass (~11:47 UTC): the 2026
Grand Final (`match_id` 9028) and both Preliminary Finals (`match_id`
9026/9027) reported `lifecycle.finality="not_available"` with zero player
rows, and this document correctly recorded season 85 as not fully
populated end-to-end and continuing to block packages 08/32. The operator
reported the upstream provider had since backfilled those three matches
from the authoritative CFS source; a second validation pass (~12:51 UTC)
confirmed all three now report `lifecycle.finality="final"` with 46
player rows each, and a full sweep of all 218 matches in season 85 (every
`GET /api/v1/matches/{id}/player-stats`, not a sample) found zero
remaining incomplete matches. **Packages 08/32 are no longer blocked by
this gap** — see §3 item 2.

### OpenAPI comparison — completed, no incompatible difference found

The live `GET /openapi.json` (`AFL-api 0.7.0`) advertises every path this
document's contract requires, including
`/api/v1/seasons/{season_id}/players` (added to the diagnostic's optional
cross-check by this issue — see "Diagnostic change" below). Differences
found, both benign and non-blocking under the
[compatibility policy](#compatibility-and-pinning-policy)'s additive-change
rule:

- New, additive endpoints BBBFFL does not consume and has no documented
  need for: `/api/v1/players/{id}/movements`,
  `/api/v1/players/{id}/seasons/{season_id}/player-stat-summary`,
  `/api/v1/seasons/{season_id}/player-stat-summaries`.
- The legacy unversioned `/api/...` routes (`/api/matches`, `/api/players`,
  etc.) remain present, matching this document's existing statement that
  they are "pre-v1 legacy behaviour, not a permanently supported parallel
  API" and are not used by BBBFFL.

### Authentication/configuration — now positively confirmed

`AFL_API_BASE_URL` (service root) and `AFL_API_KEY`, read only through
`app.config.get_settings()`, need no code or naming change to build every
request path exercised above, and the `X-Api-Key` header name and both the
unstructured 401 body and structured `{"error": {...}}` 404/422 bodies
match this document's existing source-level analysis in
[§1.7](#17-authentication-and-configuration) exactly.

**What the validating session's own run could not establish:** whether a
real, deployment-issued `AFL_API_KEY` is actually honoured end-to-end.
That session's egress proxy authenticates every outbound request to
`afl-api.thehardinghams.net` at the transport level, regardless of what
(if any) `x-api-key` header the calling code sends — every request in
that run succeeded through the proxy-injected credential, not through the
placeholder, non-secret `AFL_API_KEY` value the session actually set
locally. Consequently the diagnostic's own negative-path checks
(`GET /api/v1/seasons` with no key, and with a deliberately invalid key,
both expected to return `401`) observed `200` and were correctly recorded
as `FAIL` in that run — it could not distinguish "the deployment accepts
any key" from "the deployment enforces a real key and the proxy happened
to already be authenticated".

**Independently closed the same day:** the operator ran the equivalent
three checks directly from the BBBFFL production Docker host, on a
network path that does not auto-authenticate:

- no `X-Api-Key` header → `401`
- an invalid `X-Api-Key` value → `401`
- the real, deployment-issued `X-Api-Key` value → `200`

This is exactly the follow-up this document called for, confirms the
deployment enforces its configured credential rather than accepting any
request, and closes the previously open credential-validation gap. No key
value was shared with, or is recorded by, this document or this
session — only the pass/fail outcome above.

### UTC and status semantics — confirmed

- Every timestamp observed (`rounds[].start_time`/`.end_time`,
  `matches[].start_time_utc`) was UTC ISO 8601, using either an explicit
  `+0000` offset or a `Z` suffix depending on the field — both pass through
  `AflApiClient` unparsed as opaque strings, so the formatting difference
  is not a compatibility concern.
- `matches[].status` was `CONCLUDED` for all 218 observed 2026-season
  matches (the season has fully finished); no `LIVE`, `UPCOMING`, or
  `POSTGAME` match was available to observe directly during this
  validation window, since no 2027 season has been published by afl-api
  yet (see "Known non-blocking gap" below). The full four-value vocabulary
  itself remains confirmed by source review ([§1.3](#13-match-lifecycle))
  and by the diagnostic's `VALID_MATCH_STATES` check.
- **A concrete, live confirmation that `POSTGAME`/`CONCLUDED` and
  player-stat finality are genuinely independent signals, not aliases:**
  the 2026 Grand Final match reports `matches.status="CONCLUDED"` (with a
  final score), while its own `player-stats` resource independently
  reports `lifecycle.finality="not_available"` and zero player rows (stats
  not yet loaded for that specific match at validation time). This is
  exactly the distinction [§1.3](#13-match-lifecycle) and
  [§1.4](#14-player-stat-finality-and-corrections) already document as two
  separately-sourced facts — BBBFFL must never collapse them — now backed
  by a real, non-hypothetical example.

### Known, non-blocking gap: no season is currently flagged `is_current`

Validated the day after the 2026 AFL Grand Final (2026-09-26), the
deployment correctly reports **zero** of its 15 seasons with
`is_current=true`, and no 2027 season resource exists yet at all. This is
the same outcome `AflApiClient.get_current_season()` would itself raise
`AflApiError` on right now — it is a genuine, expected off-season timing
state (afl-api has not yet published the 2027 season), not a contract
incompatibility, and per this document's compatibility policy it is not
weakened or masked here. It is expected to resolve once afl-api publishes
the 2027 season ahead of that season's start; `app/round_preflight.py`'s
human-readable season selection (`AflApiClient.get_seasons()`) already
gives BBBFFL's Season Setup workflow (issue #237) a supported path that
does not require `is_current` to be set. See
[`docs/2027-live-season-readiness.md`](2027-live-season-readiness.md) item
11 for how this affects release readiness.

### Diagnostic change made because of this validation

Before this run, `check_seasons` treated "no season flagged
`is_current`" as a reason for every downstream check (rounds, matches,
match detail, player-stats, player identity) to `SKIP` outright — accurate
for the `is_current` check itself, but it meant the diagnostic could not
produce any of the representative-data evidence above during the exact
off-season window it was actually run in. `scripts/afl_contract_diagnostic.py`
now falls back to the most recently listed season when no season is
flagged current, so the rest of the contract is still positively exercised
with real data; the `is_current` check itself still fails honestly and is
recorded as an informational, non-required note when the fallback is used.
A separate, previously-missing required check for
`GET /api/v1/seasons/{season_id}/players` — classified **required now** by
this document since issue #237, but never exercised by the diagnostic —
was added at the same time (see the table above). Its first version
requested only a single small page (`limit=50`); a PR review (Codex,
P1) correctly pointed out that this would report a deployment compatible
even if it rejected or clamped `AflApiClient.get_season_players`'s actual
production page size (`SEASON_PLAYERS_PAGE_LIMIT`, 250), or returned
malformed/duplicate rows past the first page. The check now requests the
identical production page size and follows pagination to the terminating
short page, failing if the echoed `limit`/`offset` ever stops matching
what was requested or if a `canonical_player_id` repeats across pages.

A second Codex review pass (P1) on the same check then pointed out that
validating only which *keys* each row carries would still certify a
deployment that sends every named key but a value the production client
rejects — a null `canonical_player_id`, a blank `display_name`, a
non-string structured name, or an unresolved `team` — even though
`AflApiClient.get_season_players` raises `AflSeasonPlayersContractError`
on exactly those values. The check now runs the identical row-value
validation `get_season_players` does (reusing its own `_is_positive_int`/
`_is_optional_structured_name` predicates rather than restating the
rules) before counting a row as valid. Against the live deployment this
now positively confirms 812 players across 4 pages with no such defect.
Both fixes are covered by hermetic offline tests in
`tests/test_afl_contract_diagnostic.py`; no production application code
changed.

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
