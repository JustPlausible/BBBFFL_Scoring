# Phase 1 closeout

## Completion decision

The 2026 first-half replay is complete through BBBFFL Round 9. It is suitable
as the recovery baseline and starting point for the second-half replay.

Completion was accepted because:

- BBBFFL Rounds 1–9 are all in lifecycle state `final`;
- each round has ten authoritative submitted lineups;
- AFL rounds 1343–1352 are present in the final-results checkpoint;
- all selective and main lockout triggers have persisted activation evidence;
- scoring, manual review, publication and cumulative ladder updates completed;
- correction, adjudication and carry-forward paths were exercised with audit
  reasons;
- the closing application and migration baseline were recorded;
- the database archive and matching checkpoint were independently validated.

## Closing baseline

| Item | Recorded state |
|---|---|
| Application | `3abc503` |
| Migration | `0026_lineup_adjudication` |
| BBBFFL rounds | 1–9 |
| AFL ordinary rounds | 1344–1352 |
| Opening Round evidence | 1343 |
| Lifecycle | all nine ordinary rounds `final`, version 5 |
| Lineup completeness | 10 authoritative submissions per round |
| Checkpoint | `2026-05-10T10:15:00Z`, `final-results` |

## Recovery package

The operator retained, outside version control:

1. a PostgreSQL custom-format database archive;
2. the matching replay checkpoint JSON;
3. a SHA-256 record for both artifacts;
4. the fixed replay evidence and player-pool files;
5. final Administrator, Scorer and public-page captures.

The database catalogue was checked with `pg_restore --list`, the checkpoint
was parsed successfully, and the evidence package hashes matched the manifest.

A restore rehearsal for Phase 2 should use the database archive and checkpoint
as an inseparable pair, then confirm the migration head, application commit,
round lifecycle table and published Round 9 ladder before any new mutation.

## Evidence interpretation

The final public capture was taken after the application containers had been
stopped. Its live-refresh “Failed to fetch” message therefore records an
offline capture condition, not an application failure.

The replay intentionally preserved rule-correct data where a historical
worksheet conflicted with the previous authoritative lineup. That decision may
produce a small historical ladder variance but did not change the Round 9
matchup winner.

## Shutdown posture

The application and database may remain stopped after the validated archive is
created. Starting Phase 2 should be deliberate: restore or clone the Phase 1
baseline, verify it read-only, then begin Round 10 preparation on a separately
named second-half recovery line.
