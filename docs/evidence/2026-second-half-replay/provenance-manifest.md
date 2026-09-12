# Provenance manifest

This is the sanitised provenance record for the completed 2026 second-half
home-and-away replay. Private database archives, checkpoint JSON files, host
paths, backup filenames and integrity hashes remain in the operator's private
recovery store under the evidence policy in [README.md](README.md).

## Source first-half checkpoint

| Field | Value |
|---|---|
| Source state | Completed first-half replay through BBBFFL Round 9 |
| Paired database/checkpoint verification | PASS; retained privately |
| Checkpoint stage | `final-results` |
| First-half evidence baseline | Verified against `docs/evidence/2026-first-half-replay/` before continuation |

The second-half replay was created from a copy of the completed first-half
working database rather than a new season bootstrap, preserving the earlier
2026 submissions, results, ladder and audit history.

## Second-half working copy

| Field | Value |
|---|---|
| Working database | `bbbffl_2026_second_half` |
| Restore/migration result | PASS |
| Rounds 1–9 history retained | PASS |
| Existing squads/ownership retained | PASS |
| Existing official-result/audit history retained | PASS |

## 9-to-20-round continuation (issue #178 / PR #179)

| Field | Value |
|---|---|
| Season id | `3832745c-c19a-4224-bceb-86ded6baa09c` |
| Ordinary season length | extended from 9 to 20 rounds |
| Existing Round 1–9 identities/history | preserved |
| New ordinary rounds | Rounds 10–20 appended through supported continuation workflow |
| Idempotent status/validation | PASS |
| Raw continuation audit identifiers | retained in private operator record |

## Round 10 pre-draft boundary

| Field | Value |
|---|---|
| Round | BBBFFL Round 10 / AFL round 1353 |
| Round lifecycle | final/published |
| Mathematical ladder verified before draft | PASS |
| Paired database/checkpoint recovery point | PASS; retained privately |
| Mid-season ownership mutation before finalisation | none |

## Post-mid-season-draft boundary

| Field | Value |
|---|---|
| Season id | `3832745c-c19a-4224-bceb-86ded6baa09c` |
| Ordinary competition id | `9de8e7d3-8d56-4c5c-afd0-803b787e4055` |
| Trigger round | 10 |
| Delistings | 28 historical delistings recorded/locked |
| Generated/completed selections | 28 / 28 |
| Historical post-draft trade | James Rowbottom to Bridesmaids; Lachlan McAndrew to The Crabs |
| Final squad sizes | 10 teams × 22 players — PASS |
| Post-draft trading | closed before Round 11 |
| Corrected post-draft database/team-list evidence | retained privately and SHA-256 verified |

The historical trade was recovered from the preserved post-draft/pre-close
boundary rather than patched into the canonical database. The exact restored
archive and private hashes are intentionally not committed.

## Routine second-half recovery boundary

| Field | Value |
|---|---|
| Boundary | Round 16 complete |
| Checkpoint stage | `final-results` |
| Finalised AFL round ids at boundary | 1353–1359 |
| Paired database/checkpoint backup | PASS |
| Archive readability check | PASS (`pg_restore --list`) |
| Private checksum verification | PASS |

This additional recovery point was retained before the final four ordinary
rounds to reduce the risk of repetitive manual replay entry.

## Round 20 / home-and-away boundary

| Field | Value |
|---|---|
| Completed ordinary rounds | 20 |
| Round 20 lifecycle | final/published |
| Mathematical ladder reproducible | PASS; recorded in `round-results.md` |
| Earlier 2026 history retained | PASS |
| End-of-home-and-away database/checkpoint snapshots | retained privately before finals-seeding apply |
| Known material historical divergence | Round 12 and Round 13 winner-changing Scorer errors; documented in `finals-seeding-2026.md` |

This paired restore point is the recovery boundary for the handoff to finals and
SuperScore planning under issue #170.

## Finals-seeding snapshot (issue #187 / PR #188)

| Field | Value |
|---|---|
| Season id | `3832745c-c19a-4224-bceb-86ded6baa09c` |
| Ordinary competition id | `9de8e7d3-8d56-4c5c-afd0-803b787e4055` |
| Snapshot id | `a5ac1c3d-7dde-446a-aca4-4defe15b8ff7` |
| `finals_seeding.snapshot.created` audit event id | `53799ad0-f32f-4cbe-a78e-5674d81d5c34` |
| Mathematical Round 20 ladder mutated | no |
| Historical seed order | Running Hots, Bridesmaids, JHAS, Wolverines, Evil Absolutes, The Crabs, One Percenters, Motherruckers, Pommy Rules, The Plague |
| Replay-context validation | PASS |
| Snapshot re-read/preview after apply | PASS; existing snapshot resolved deterministically |

The snapshot exists only to reproduce the historical 2026 finals order while
preserving the mathematical replay as separate evidence. It is structurally
restricted to the 2026 replay and is not a live-season ladder editor.

## AFL evidence acquisition

The second-half AFL evidence package was acquired and validated before replay
execution, then used as the isolated provider source for the ordinary second
half. It covered the AFL rounds required for BBBFFL Rounds 10–20 and later
finals/SuperScore preparation. Package identity, acquisition metadata and raw
validation output remain in the private operator evidence record; no
credentials are stored here.

## Known replay deviations / unresolved historical uncertainty

- Small PF/PA/statistical differences exist between current authoritative AFL
  evidence and the historical spreadsheet. They were not manually altered when
  they did not change a matchup winner.
- The two winner-changing historical Scorer errors are fully identified and
  documented. They are represented for finals purposes by the audited
  finals-seeding snapshot rather than by rewriting the mathematical Round 20
  ladder.
- Historical bye-player selections in Round 12 are represented through audited
  lineup-correction versions; production ordinary submission remains stricter
  and correctly prevents those selections.
