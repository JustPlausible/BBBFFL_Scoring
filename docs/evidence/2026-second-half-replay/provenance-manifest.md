# Provenance manifest

This is the sanitised provenance record for the completed 2026 second-half
home-and-away replay. Private database archives, checkpoint JSON files, host
paths, backup filenames and integrity hashes remain in the operator's private
recovery store under the evidence policy in [README.md](README.md).

## Source first-half checkpoint

| Field | Value |
|---|---|
| Source state | Completed first-half replay through BBBFFL Round 9 |
| Source application baseline | `3abc503` |
| Source database migration | `0026_lineup_adjudication` |
| Paired database/checkpoint verification | PASS; private SHA-256 verification retained by operator |
| Checkpoint effective time / stage | `2026-05-10T10:15:00Z` / `final-results` |
| Finalised AFL round ids | 1343–1352 |
| First-half evidence baseline | Verified against `docs/evidence/2026-first-half-replay/` before continuation |

The second-half replay was created from a copy of the completed first-half
working database rather than a new season bootstrap, preserving the earlier
2026 submissions, results, ladder and audit history. The private source archive
and matching checkpoint remain integrity-bound by the operator's retained
SHA-256 manifest; repository documentation records their executable/schema and
checkpoint identity without publishing private recovery filenames or hashes.

## Second-half working copy

| Field | Value |
|---|---|
| Working database | `bbbffl_2026_second_half` |
| `restored_from` | verified first-half Round 9 closeout pair described above (`2026-05-10T10:15:00Z`, `final-results`, AFL rounds 1343–1352) |
| Application commit at restore | `3abc503` (confirmed by local reflog; restore archive created before the subsequent pull away from this commit) |
| Migration head at restored baseline | `0026_lineup_adjudication` (confirmed by restoring the pre-migration archive into an isolated PostgreSQL database and running `python -m app.migrations current`) |
| Database engine | PostgreSQL 16 |
| Replay-clock assumption | replay time is supplied by the paired replay checkpoint; database and checkpoint are treated as one recovery boundary |
| Provider-evidence assumption | second-half AFL evidence is acquired/validated ahead of replay and replay execution is isolated from live provider mutation |
| Rounds 1–9 history retained | PASS |
| Existing squads/ownership retained | PASS |
| Existing official-result/audit history retained | PASS |

A separate private pre-Round-10 recovery pair was then captured after the
supported continuation/migration preparation. Its replay checkpoint was
`2026-05-10T10:15:00Z`, stage `scheduled`, with no second-half AFL rounds yet
finalised. This distinguishes the original restored Round 9 baseline from the
ready-to-enter-Round-10 working state.

## 9-to-20-round continuation (issue #178 / PR #179)

| Field | Value |
|---|---|
| Season id | `3832745c-c19a-4224-bceb-86ded6baa09c` |
| Fixture draw id | `433ec8d9-a585-4cea-b5fe-d9cfdcabd40b` |
| Fixture draw version after continuation | `3` |
| Ordinary season length | extended from 9 to 20 rounds |
| Existing Round 1–9 identities/history | preserved |
| New ordinary rounds | Rounds 10–20 appended through supported continuation workflow |
| Rotation version | `bbbffl-workbook-2026-v1` |
| `replay.season.continued` audit event | `c0cd5471-90a2-422c-8487-ada9d2eed837` |
| `fixture.draw.continued` audit event | `f081c8f1-dc5f-47f6-857c-a8d123c51e0f` |
| Continuation reason | `2026 second-half replay continuation before Round 10` |
| Idempotent status/validation | PASS |

The continuation audit payload records preserved rounds 1–9, appended rounds
10–20, creation of logical `round-10` through `round-20`, fixture draw version
3 and the workbook rotation version above.

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
| Replay checkpoint effective time / stage | `2026-07-26T11:15:00Z` / `final-results` |
| Finalised second-half AFL round ids | 1353–1363 |
| End-of-home-and-away database/checkpoint snapshots | retained privately before finals-seeding apply |
| Application handoff commit | `6fe937cf16343f3c0b2d0f3accd4a0168450441f` (`bbbffl:6fe937c`) |
| Migration head at finals handoff | `0028_finals_seeding` |
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
