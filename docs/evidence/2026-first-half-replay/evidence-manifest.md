# Evidence manifest

This manifest identifies the reproducible, non-secret inputs and implementation
milestones used by the 2026 first-half replay. Evidence files, database dumps,
private configuration and checkpoint files are deliberately not committed.

## AFL evidence package

| Item | Value |
|---|---|
| Package schema | `bbbffl.first-half/v1` |
| Manifest identifier | `afl-2026-first-half` |
| AFL season | `85` (year 2026) |
| AFL rounds | `1343`–`1352` |
| Match count | 81 |
| Final-stat coverage | 81/81 matches |
| Roster coverage | 81/81 matches |
| Player population | 812 |
| Evidence SHA-256 | `bad03c2c417822e77e873b779e043b788d9c6bc4a1913e2ad20cb8d3e2b7f242` |
| Player-pool SHA-256 | `d06a34a0188081277659bf36a7674514dfbeeafbac21034a99d3f080640f7072` |

The package is consumed in replay mode from a read-only mount. A separately
writable checkpoint advances effective time and releases final evidence without
altering the package.

## Relevant implementation milestones

| Capability | Public reference | Replay milestone |
|---|---|---|
| Opening Round bootstrap | PR #129, merge `a9de656` | Draft/preseason reconstruction |
| Opening Round multi-player rule | PR #136, merge `32f5e80`; migration `0024_opening_round_multi_player` | All sixty nominations accepted |
| Audited locked-lineup correction | PR #142, merge `aee6d9e`; migration `0025_lineup_correction` | Exercised in Rounds 2 and 4 |
| Delegated staged-lock presentation | PR #143, merge `a26fea2` | Exercised from Round 3 |
| Live unlocked-position submission | PR #145, merge `b665ab5` | Verified Round 3 onward |
| Missed-initial-submission adjudication | PR #149, merge `5ce7496`; migration `0026_lineup_adjudication` | Workflow inspected and retained; not misused for unsupported reconstruction evidence |

## Replay integrity controls

- Replay operates against an isolated PostgreSQL database and fixed evidence
  package.
- Checkpoint time is monotonic during ordinary progression.
- Trigger activations and lineup submissions are durable database facts.
- Published rounds are immutable through ordinary round workflows.
- Audited correction/adjudication creates new immutable history instead of
  rewriting prior submissions.
- Database restoration uses a matching dump/checkpoint pair and is followed by
  migration and lifecycle verification.
- Current source statistics are not modified merely to reproduce a legacy
  spreadsheet value.

## Deliberately excluded evidence

The complete operator journal retains additional deployment commands,
timestamps, screenshots and reconstruction notes outside Git. Raw private
communications, authentication data, internal routing, personal identifiers,
database dumps and host-level backup details are not suitable repository
evidence.
