# Evidence manifest

## Fixed replay package

| Field | Value |
|---|---|
| Evidence schema | `bbbffl.first-half/v1` |
| Manifest | `afl-2026-first-half` |
| AFL season | 85 |
| AFL rounds present | 1343–1352 |
| AFL matches represented | 81 |
| Player-stat match records | 81 |
| Match-roster records | 81 |
| Season player-pool rows | 812 |
| Evidence package SHA-256 | `bad03c2c417822e77e873b779e043b788d9c6bc4a1913e2ad20cb8d3e2b7f242` |
| Player-pool SHA-256 | `d06a34a0188081277659bf36a7674514dfbeeafbac21034a99d3f080640f7072` |

The package hashes are safe to publish because they identify the fixed
non-secret replay inputs. Backup/checkpoint filenames and their integrity
hashes remain in the operator's private recovery record.

## Closing runtime baseline

| Field | Value |
|---|---|
| Application commit | `3abc503` |
| Database migration | `0026_lineup_adjudication` |
| Replay checkpoint time | `2026-05-10T10:15:00Z` |
| Replay checkpoint stage | `final-results` |
| Finalised AFL round IDs | 1343–1352 |
| BBBFFL lifecycle rows | Rounds 1–9, all `final`, version 5 |
| Authoritative weekly lineups | 10 of 10 in every round |

## Implementation milestones

| Capability | Public reference | Replay milestone |
|---|---|---|
| Opening Round bootstrap | PR #127, merge `f92e6af` | Draft/preseason reconstruction |
| First-half replay source and staged lockout | PR #136, merge `32f5e80` | Round 1 start |
| Audited locked-lineup correction | PR #142, merge `aee6d9e` | Round 2 |
| Human-readable delegated lineup values | PR #143, merge `a26fea2` | Round 2–3 |
| Live-round remaining-position submissions | PR #145, merge `b665ab5` | Round 3 |
| Missed-initial-submission adjudication | PR #149, merge `5ce7496` | Round 3–4 |
| Human-readable operational identities | PR #154, merge `87e06d5` | Round 5 |
| Authoritative post-mutation refresh | PR #156 | Round 6 onward |
| Main-lock vacant-position presentation | PR #157 | Round 5 onward |
| Guided round preflight | PR #158 | Round 6 onward |
| Scorer Operations Dashboard | PR #159 | Round 6 onward |
| Administrator governance dashboard | PR #160, merge `3abc503` | Closing baseline |

## Integrity controls

- The replay data source validates its own schema and content hashes before use.
- Opening Round inputs preserve provenance, actor type and nomination intent.
- Lockout activation is represented by persisted trigger evidence.
- Submissions, corrections, adjudications, calculations, manual rulings and
  publication remain versioned and auditable.
- The closing database archive was created in PostgreSQL custom format and its
  catalogue was validated with `pg_restore --list`.
- The matching checkpoint JSON was syntax-validated.
- Private SHA-256 records bind the database archive, checkpoint and retained
  page captures without publishing deployment-specific filenames.

## Excluded repository evidence

The following remain outside version control:

- raw private league messages;
- screenshots and PDFs containing private deployment details;
- database dumps and replay checkpoint files;
- host filesystem paths and private network addresses;
- credentials, session values and administrative tokens.

Their behavioural conclusions are represented in the sanitised documents in
this directory.
