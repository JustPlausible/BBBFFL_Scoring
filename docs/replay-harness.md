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
`player_stats` keyed by match. Every season, round, match, player, stat line,
and historical lineup has its own required `provenance` object containing a
source and one of the stable identifiers `known_fact`,
`reconstructable_behaviour`, `synthetic_scenario`, or
`unresolved_scorer_input`. A single run can therefore mix classifications.
Round 1's AFL mapping, reconstructed lineups, and synthetic player/stat facts
remain distinct; synthetic inputs must not be presented as historical coach
selections.

Lineup submissions retain the represented `season_entry_id` while actor
columns record `anonymous_operator`/`scorer` and `source_type=scorer_proxy`.
Thus the replay operator is never impersonated as the historical coach.

`ReplayClock` requires an explicit timezone-aware instant. The manifest's
match-status timeline is evaluated at that instant and the same instant is
passed to existing lockout `evaluation_at` seams. The acceptance path uses
the one file-backed source before lockout, during play, and after conclusion;
neither captured AFL evidence nor global/wall time is changed.

## Scoring sources and #69

Replay AFL evidence is keyed by AFL round and match, not by the current
BBBFFL round. Calculation remains `MatchupCalculationService`, whose slot
evidence persists `scoring_source`, `source_afl_round_id`, and (for accepted
Opening Round nominations) `opening_round_deferred` provenance. Reports must
copy those fields rather than infer every slot to be ordinary. A missing
deferred source therefore fails through #69's production resolver rather
than becoming a bye, DNP, or replay-supplied score.

## Reports and discrepancies

`build_completed_round_report` reads the final production round, calculation
snapshots, official result versions, and ladder, after which
`write_replay_report` emits deterministic sorted JSON and a text summary. The
acceptance test generates this real report twice from two clean databases. A
report includes run/configuration, mapping, lineups, validation, lockout,
scoring, scorer workflow, official results, ladder, evidence, and discrepancy
sections.
Expected/actual mismatches belong in `discrepancies` with their evidence
class and category; fixture evidence must never be rewritten to hide one.

For #67, add another immutable manifest, seed its scenario-specific initial
facts at the same boundary, supply explicit clock instants, and reuse the
same production services/report sections. Do not add a replay scoring path.

## Checkpoint suite (issue #67)

Issue #67's staged/early-lockout, bye/availability, missing-submission/
carry-forward/proxy, and Opening Round deferred/compensating-bye checkpoint
scenarios, their evidence fixtures, and the Rounds 1-9 operator procedure
live in [`docs/replay-checkpoint-2026.md`](replay-checkpoint-2026.md) and
`bbbffl_app/tests/test_replay_checkpoint.py`. They extend this document's
report/provenance conventions via `app.replay_checkpoint`
(`build_checkpoint_scenario`/`build_checkpoint_suite`/
`write_checkpoint_suite_report`) rather than replacing them -- read that
document first if you are preparing the real Rounds 1-9 replay.
