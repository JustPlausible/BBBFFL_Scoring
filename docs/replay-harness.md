# One-round 2026 replay harness

Issue #66's first replay checkpoint uses **BBBFFL Round 1 / AFL Round 1
(AFL round id 1344)**. It is an ordinary weekly round, chosen to prove the
ten-lineup/five-matchup vertical without pre-empting #67's specialist early
lockout, bye and non-submission cases. The harness is round-data driven; a
future manifest may name a different round.

## Configuration and entry points

Set `BBBFFL_AFL_MODE=replay` and
`BBBFFL_AFL_REPLAY_EVIDENCE_PATH=/absolute/path/evidence.json`. Application
startup constructs `ReplayAflDataSource` rather than `AflApiClient`; the
replay object has no HTTP transport or fallback. Missing, malformed,
incomplete, cross-referenced, or unsupported evidence fails startup closed.
Live mode is unchanged.

The checked-in representative evidence is
`bbbffl_app/tests/fixtures/replay_round_2026/evidence.json`. From a clean
checkout/database, execute the deterministic service/test entry point:

```bash
cd bbbffl_app
pytest -q tests/test_replay.py tests/test_replay_round.py tests/test_startup.py
```

The vertical test initialises an isolated migrated relational database,
loads the minimum synthetic season/player/ownership/fixture/lineup state,
and then invokes production validation, effective-time lockout, calculation,
round review/sign-off, immutable official results, and ladder services. The
fixture-load boundary is the only direct seed operation.

## Evidence schema and provenance

The root schema is `bbbffl.replay-evidence/v1`. It contains a versioned
`manifest`, AFL `seasons`, `rounds`, `matches`, canonical `players`, and
`player_stats` keyed by match. `manifest.evidence_class` is one of the stable
identifiers `known_fact`, `reconstructable_behaviour`, `synthetic_scenario`,
or `unresolved_scorer_input`. Round 1's compact fixture is explicitly
synthetic; it must not be presented as a historical coach selection.

Lineup submissions retain the represented `season_entry_id` while actor
columns record `anonymous_operator`/`scorer` and `source_type=scorer_proxy`.
Thus the replay operator is never impersonated as the historical coach.

`ReplayClock` requires an explicit timezone-aware instant. The same instant
is passed to existing lockout `evaluation_at` seams; neither captured AFL
facts nor global/wall time are changed.

## Scoring sources and #69

Replay AFL evidence is keyed by AFL round and match, not by the current
BBBFFL round. Calculation remains `MatchupCalculationService`, whose slot
evidence persists `scoring_source`, `source_afl_round_id`, and (for accepted
Opening Round nominations) `opening_round_deferred` provenance. Reports must
copy those fields rather than infer every slot to be ordinary. A missing
deferred source therefore fails through #69's production resolver rather
than becoming a bye, DNP, or replay-supplied score.

## Reports and discrepancies

`write_replay_report` emits deterministic sorted JSON and a text summary. A
report must include run/configuration, mapping, lineups, lockout, scoring,
scorer workflow, official results, ladder, and discrepancy sections.
Expected/actual mismatches belong in `discrepancies` with their evidence
class and category; fixture evidence must never be rewritten to hide one.

For #67, add another immutable manifest, seed its scenario-specific initial
facts at the same boundary, supply explicit clock instants, and reuse the
same production services/report sections. Do not add a replay scoring path.
