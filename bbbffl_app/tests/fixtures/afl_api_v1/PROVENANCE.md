# afl-api `/api/v1` contract fixtures — provenance

## Source and method

These fixtures were **not** captured from a live HTTP response. This
session's outbound network access was blocked by organisation egress policy
for `afl-api.thehardinghams.net` (confirmed via the local agent proxy's
`__agentproxy/status`, which recorded a `connect_rejected` / gateway `403`
for that host), so the deployed dev instance and its `/openapi.json` could
not be reached from this environment.

Per the priority order in issue #18 ("AFL-api source, tests and
documentation" ranks above "controlled read-only requests to the deployed
service"), these fixtures were instead constructed directly from the
**upstream AFL-api source code and documentation**, which is the higher
authority in that ordering:

- Repository: `https://github.com/JustPlausible/AFL-api`
- Commit: `af4bf93f50140fa7d1465a446c63588abc5e376c` (`main`, cloned read-only
  for this investigation)
- Release version at that commit: `0.7.0` (`version.py`)
- Primary sources per fixture: the exact Pydantic response models and route
  handlers in `api/routes_v1.py`, cross-checked against
  `docs/api_v1_seasons.md`, `docs/api_v1_rounds.md`, `docs/api_v1_matches.md`,
  `docs/api_v1_player_stats.md`, `docs/api_v1_players.md`,
  `docs/api_v1_rosters.md`, `docs/api_v1_injuries.md`, and
  `docs/architecture/workflows/consumer_api_design.md`.
- Match lifecycle vocabulary (`UPCOMING`/`LIVE`/`POSTGAME`/`CONCLUDED`) is
  confirmed authoritative by `afl_json/match_period.py`'s module docstring:
  "`afl_json.match_status`/`matches.status` remain the sole source of truth
  for `UPCOMING`/`LIVE`/`POSTGAME`/`CONCLUDED`."

Field names, types, nullability, wrapper keys, ordering rules, and error
shapes in every fixture below are copied exactly from that source. Values
(IDs, names, scores, timestamps) are illustrative and synthetic, chosen to
exercise the semantic distinctions BBBFFL depends on (see
`docs/afl-api-v1-contract.md`), except where noted as reused directly from
an upstream worked example (`docs/api_v1_players.md`'s Nick/Josh Daicos
example) for continuity with an authoritative source example.

**This is a documented gap, not a substitute for live evidence.** Before
package 08 (deterministic AFL evidence fixtures) or the 2026 replay
(packages 32-36) proceed, the opt-in diagnostic in
`scripts/afl_contract_diagnostic.py` must be run from an environment with
network access to the configured `afl-api` deployment, and any live-capture
discrepancy against these source-derived fixtures must be resolved before
trusting them further. See `docs/afl-api-v1-contract.md` for the tracked
follow-up.

## Sanitisation note

No fixture in this directory contains a real API key, real coach/scorer
personal data, or any BBBFFL-side state. Player/team names are public AFL
identities (the same public figures already used in `tests/test_afl_client.py`
and in AFL-api's own published documentation examples); no other personal
data is represented.

## File index

| File | Endpoint | What it demonstrates |
| --- | --- | --- |
| `api_discovery.json` | `GET /api/v1` | Discovery envelope shape. |
| `seasons.json` | `GET /api/v1/seasons` | Current + historical seasons; nullable `current_round_number`. |
| `rounds_season_85.json` | `GET /api/v1/seasons/85/rounds` | Explicit empty `byes: []`, populated byes, and `byes: null` (unresolved) in one season. |
| `rounds_season_84_historical.json` | `GET /api/v1/seasons/84/rounds` | Historical (non-current) season round access. |
| `matches_round_1412_lifecycle.json` | `GET /api/v1/rounds/1412/matches` | One of each lifecycle status: `UPCOMING`, `LIVE`, `POSTGAME`, `CONCLUDED`, in one round. |
| `match_8504_detail.json` | `GET /api/v1/matches/8504` | Single canonical match projection. |
| `player_stats_8503_postgame_partial.json` | `GET /api/v1/matches/8503/player-stats` | `lifecycle.finality: "partial"`; a row with unresolved `canonical_player_id`; a *resolved* player with one still-null individual stat field (mid-collection) alongside populated ones — the null-vs-zero case that exposes a documented BBBFFL adapter gap (see `docs/afl-api-v1-contract.md`). |
| `player_stats_8504_concluded_final.json` | `GET /api/v1/matches/8504/player-stats` | `lifecycle.finality: "final"`; fully resolved rows used for scoring. |
| `player_396.json` | `GET /api/v1/players/396` | Canonical player + current-season team + identifier crosswalk. |
| `player_584_seasons.json` | `GET /api/v1/players/584/seasons` | Season/team membership history where the same player changes club across seasons (from AFL-api's own documented worked example). |
| `players_search_daicos.json` | `GET /api/v1/players?search=daicos` | Name search / identity discovery. |
| `injuries.json` | `GET /api/v1/injuries` | Current injuries, including an unresolved team crosswalk. |
| `rosters_match_8504.json` | `GET /api/v1/matches/8504/rosters` | Selections vs. context (ins/outs/late changes) as distinct collections; selection is not participation evidence. |
| `error_404_match_not_found.json` | any `/api/v1/matches/{id}...` | Structured v1 application-error shape. |
| `error_401_unauthenticated.json` | any `/api/v1/...` | Auth-layer error shape — deliberately different from the structured shape above. |
| `error_403_advanced_access_required.json` | `GET /api/v1/matches/{id}/player-stats?advanced=true` | Capability-gated error. |
| `error_422_search_required.json` | `GET /api/v1/players?search=` | Blank-parameter application error. |
