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

## Post-finals-seeding-apply boundary

**Status: historical evidence gap.** The replay did proceed from the
applied finals-seeding snapshot into a completed Finals/SuperScore phase,
but this closeout pass does not have the exact post-seeding backup
filename/hash, application commit, migration head or readability output
available to verify retrospectively. Those values are therefore recorded
as unavailable rather than invented. The earlier Round 20 source boundary
and the later pre-closeout/final archival boundaries remain preserved.

| Field | Value |
|---|---|
| Boundary | Immediately after `scripts.finals_seeding_2026 ... apply` (already run, per the row above) |
| Paired database backup taken | not verifiable from retained closeout record |
| Backup filename (private, not committed) | unavailable in retained closeout record |
| Backup SHA-256 (private, not committed) | unavailable in retained closeout record |
| Checkpoint JSON filename (private, not committed) | unavailable in retained closeout record |
| Checkpoint JSON SHA-256 (private, not committed) | unavailable in retained closeout record |
| Application commit (`git rev-parse HEAD`) | unavailable in retained closeout record |
| Migration head (`python -m app.migrations current`) | unavailable in retained closeout record |
| `pg_restore --list` readability check | not verifiable retrospectively |

## Finals bracket creation

| Field | Value |
|---|---|
| Finals competition id | `bff233df-42a7-4d6f-bc4a-ea64480dbb0a` |
| Bracket id | `73a193a3-6702-41df-9e72-edfa5095f5c5` |
| `finals.bracket.created` audit event id | not retained in closeout record |
| Seed source (`snapshot`/`ladder`) | `snapshot` — historical finals-seeding snapshot `a5ac1c3d-7dde-446a-aca4-4defe15b8ff7` |

## Finals week checkpoints (one row per week, 1-4)

Each week `N`'s paired backup is the same `after-round-<N>.dump`/
`checkpoint-after-round-<N>.json` pair recorded in the SuperScore table
below -- the playbook takes one checkpoint per round covering both streams
(`2026-finals-superscore-playbook.md` section D.3.h), not a separate file
per stream.

| Week | Round id | Lifecycle final | `finals.round.finalized` event id | Paired backup taken | Backup/checkpoint filenames (private) |
|---|---|---|---|---|---|
| 1 | `d522fb1d-754a-4f38-bb41-517fb95d377a` | `final` | not retained in closeout record | not verifiable retrospectively | private/not retained here |
| 2 | `24e12fd8-ace3-4900-a787-96daed4ac6db` | `final` | not retained in closeout record | not verifiable retrospectively | private/not retained here |
| 3 | `e622a000-6202-4ad4-bb6c-9cc61130f361` | `final` | not retained in closeout record | not verifiable retrospectively | private/not retained here |
| 4 (Grand Final) | `9554e5af-2362-45be-ab0b-5edb9275f5bb` | `final` | not retained in closeout record | pre-closeout recovery checkpoint retained after full replay | private/not committed |

Record any `finals.result.corrected`/`finals.bracket.rewound` event here
too, with its reason and the affected week(s), when one occurs.

## SuperScore stream/round checkpoints

SS`N`'s "Paired backup taken" is the same `after-round-<N>.dump`/
`checkpoint-after-round-<N>.json` pair as finals week `N`'s row above --
one checkpoint per round, taken once both streams' round `N` results are
published and the bracket has advanced.

| Field | Value |
|---|---|
| SuperScore competition id | `fd3910b8-3243-4e46-a508-64d738f17272` |
| `superscore.stream.created` audit event id | not retained in closeout record |

| Round | Round id | AFL mapping (season/round) | `review_state_created` event id | Lifecycle final | Leaderboard published (version) | Paired backup taken |
|---|---|---|---|---|---|---|
| SS1 | `e66cf88c-82eb-4a54-96da-fce6052faf0b` | concurrent Finals Week 1 mapping | not retained in closeout record | `final` | published; exact version not retained | not verifiable retrospectively |
| SS2 | `96854834-d9b0-49d4-a78e-4462883d485a` | concurrent Finals Week 2 mapping | not retained in closeout record | `final` | published; exact version not retained | not verifiable retrospectively |
| SS3 | `272cf9fb-0b98-42aa-a6dc-c507fa89e2e0` | concurrent Preliminary Final mapping | not retained in closeout record | `final` | published; exact version not retained | not verifiable retrospectively |
| SS4 | `292f72f7-0a01-4a00-b4ef-9f5251968852` | concurrent Grand Final mapping | not retained in closeout record | `final` | published; exact version not retained | pre-closeout recovery checkpoint retained after full replay |

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

The replay was executed as a reconstruction against the operator's
historical 2026 records, and the Grand Final and SS4 results were confirmed
to match those records. The full week-by-week historical workbook/result
evidence was not committed to this repository, so this manifest does not
claim exact historical lineup provenance for every Finals/SS round.

A separate known home-and-away deviation remains intentionally preserved:
the mathematical Round 20 ladder differs from the historical finals order
because of two known historical Scorer-error outcomes in Rounds 12 and 13.
The historical finals-seeding snapshot was therefore used without rewriting
the mathematical ladder.

The main remaining provenance gap is recovery-boundary detail for the
post-seeding and individual Finals/SS rounds: exact backup filenames,
hashes and several audit-event ids were not retained in the closeout record.
That gap is documented here explicitly. It does not affect the verified
terminal state: all eight Finals/SuperScore rounds were final before
completion, a completed-but-not-yet-closed recovery checkpoint was taken,
the season completion/archival guard passed, and the final archive was
validated.
