# 2026 finals/SuperScore replay provenance manifest

Template and record for this phase's checkpoint boundaries, in the same
style as [`2026-second-half-replay/provenance-manifest.md`](../2026-second-half-replay/provenance-manifest.md).
Fill in each `<...>` placeholder as the corresponding step actually runs
against the real 2026 replay database -- do not mark a boundary complete
until the operator has genuinely executed it and verified the evidence
below. Database archives, checkpoint JSON files, backup filenames, and
their private SHA-256 hashes are never committed; only their identity,
metadata and verification state are recorded here.

## Starting point: inherited from the second-half replay

| Field | Value |
|---|---|
| Season id | `3832745c-c19a-4224-bceb-86ded6baa09c` |
| Ordinary competition id | `9de8e7d3-8d56-4c5c-afd0-803b787e4055` |
| Source checkpoint | Round 20 / home-and-away boundary, [`2026-second-half-replay/provenance-manifest.md`](../2026-second-half-replay/provenance-manifest.md) |
| Finals-seeding snapshot id | `a5ac1c3d-7dde-446a-aca4-4defe15b8ff7` (issue #187 / PR #188) |
| Historical seed order | Running Hots (1), Bridesmaids (2), JHAS (3), Wolverines (4), Evil Absolutes (5); The Crabs, One Percenters, Motherruckers, Pommy Rules, The Plague eliminated at seeding |

This phase never rewrites or restarts the Round 20 checkpoint above; every
step below operates on a working copy that continues directly from it,
under the same preserve-source/copy-forward discipline the first-half →
second-half transition used.

## Post-finals-seeding-apply boundary (outstanding)

**Status: OUTSTANDING -- not yet taken against the real 2026 replay
database.** Codex review on PR #196 (twenty-first round) found an earlier
design-document draft incorrectly claimed this backup already existed;
`2026-second-half-replay/provenance-manifest.md`'s "Round 20 / home-and-away
boundary" row only records a backup taken **before** finals-seeding apply.
Issue #194 must not repeat that mistake: this table row stays `OUTSTANDING`
until the operator has genuinely run the procedure in
`docs/2026-finals-superscore-playbook.md` section C and filled in the
fields below from real command output.

| Field | Value |
|---|---|
| Boundary | Immediately after `scripts.finals_seeding_2026 ... apply` (already run, per the row above) |
| Paired database backup taken | `<PASS/PENDING>` |
| Backup filename (private, not committed) | `<...>` |
| Backup SHA-256 (private, not committed) | `<...>` |
| Checkpoint JSON filename (private, not committed) | `<...>` |
| Checkpoint JSON SHA-256 (private, not committed) | `<...>` |
| Application commit (`git rev-parse HEAD`) | `<...>` |
| Migration head (`python -m app.migrations current`) | `<...>` |
| `pg_restore --list` readability check | `<PASS/PENDING>` |

## Finals bracket creation

| Field | Value |
|---|---|
| Finals competition id | `<finals_competition_id>` |
| Bracket id | `<bracket_id>` |
| `finals.bracket.created` audit event id | `<...>` |
| Seed source (`snapshot`/`ladder`) | `<...>` |

## Finals week checkpoints (one row per week, 1-4)

Each week `N`'s paired backup is the same `after-round-<N>.dump`/
`checkpoint-after-round-<N>.json` pair recorded in the SuperScore table
below -- the playbook takes one checkpoint per round covering both streams
(`2026-finals-superscore-playbook.md` section D.3.h), not a separate file
per stream.

| Week | Round id | Lifecycle final | `finals.round.finalized` event id | Paired backup taken | Backup/checkpoint filenames (private) |
|---|---|---|---|---|---|
| 1 | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` |
| 2 | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` |
| 3 | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` |
| 4 (Grand Final) | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` |

Record any `finals.result.corrected`/`finals.bracket.rewound` event here
too, with its reason and the affected week(s), when one occurs.

## SuperScore stream/round checkpoints

SS`N`'s "Paired backup taken" is the same `after-round-<N>.dump`/
`checkpoint-after-round-<N>.json` pair as finals week `N`'s row above --
one checkpoint per round, taken once both streams' round `N` results are
published and the bracket has advanced.

| Field | Value |
|---|---|
| SuperScore competition id | `<superscore_competition_id>` |
| `superscore.stream.created` audit event id | `<...>` |

| Round | Round id | AFL mapping (season/round) | `review_state_created` event id | Lifecycle final | Leaderboard published (version) | Paired backup taken |
|---|---|---|---|---|---|---|
| SS1 | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` |
| SS2 | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` |
| SS3 | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` |
| SS4 | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` | `<...>` |

Record any `superscore.leaderboard.corrected` event here too.

## Season completion (issue #195, steps 1-6)

| Field | Value |
|---|---|
| `preview_complete_season` (`scripts.season_completion_2026 preview`) result | `ready: true`, diagnostic `null` after the replay season was explicitly transitioned `setup -> active` during closeout |
| `complete_season` run (`scripts.season_completion_2026 complete`) | **PASS** — 2026-09-19 |
| `completed_season_version` | `3` |
| `completion_event_id` (`season.completed`) | `9cd65eee-d6ec-43b5-bcb7-23b275ac227a` |
| Premiership award id / season_entry_id | `53c3e248-ecba-4684-a01d-fe72ed34dda3` / `9434d644-a90e-4df2-89e6-b770b0c492df` (Evil Absolutes) |
| Wooden spoon award id / season_entry_id | `f99b3e49-54bc-4196-9d99-1826e116312f` / `cd58f124-d5d9-4201-b12d-e2aff0ada108` (The Plague) |

### Closeout lifecycle finding

The first completion preview correctly refused with `season must be active to complete (currently 'setup')`. The historical replay bootstrap had left the season lifecycle in `setup` even though the full operational replay had proceeded. Before completion, the operator used the supported `SeasonRepository.transition_lifecycle` path with an audited replay-operator reason to transition the season `setup -> active`; the season became version 2. A second completion preview then returned `ready: true` with all four Finals and all four SuperScore rounds `final`. This is retained as a replay finding: future/live season setup should make activation an explicit operational gate rather than discovering it at closeout.

## Final archival checkpoint (issue #194, step 7 -- only after the row above)

**Do not fill in this section until `scripts.season_archival_checkpoint_2026
verify` has printed a successful report.** Its `completed_season_version`/
`completion_event_id` must match the season-completion row above exactly.

| Field | Value |
|---|---|
| `verify` run and passed | **PASS** — 2026-09-19T09:27:07.499114+00:00 |
| `completed_season_version` (from `verify`, must match the row above) | `3` |
| `completion_event_id` (from `verify`, must match the row above) | `9cd65eee-d6ec-43b5-bcb7-23b275ac227a` |
| Completion event occurred at | `2026-09-19T09:24:39.991606+00:00` |
| Paired database backup taken (after `verify` passed) | **PASS** — 2026-09-19T09:28:07Z |
| Backup filename (private, not committed) | retained privately; not committed |
| Backup SHA-256 (private, not committed) | verified `OK`; retained privately |
| Checkpoint JSON filename (private, not committed) | retained privately; not committed |
| Checkpoint JSON SHA-256 (private, not committed) | verified `OK`; retained privately |
| `pg_restore --list` readability check | **PASS** |

This is the terminal recovery point for the completed 2026 season, retained
alongside the first-half/second-half checkpoints as historical league
evidence ahead of the 2027 live season.

## Known replay deviations / unresolved historical uncertainty

Carried forward from `docs/2026-finals-superscore-design.md`'s "Historical
gaps requiring Steve's confirmation" (items 1-2, still open at the time
issue #194 was written): no recovered historical evidence for the real 2026
finals-week or SuperScore SS1-SS4 lineups/results has been supplied to this
repository. Unless/until such evidence is supplied, this replay phase
necessarily runs as a fresh (evidence-free) simulation forward from the
confirmed seed, not a reconstruction of actual 2026 results -- record
whichever applies here once the operator begins execution.
