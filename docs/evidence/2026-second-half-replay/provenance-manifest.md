# Provenance manifest (template)

Fill in each row as the corresponding boundary in
[`docs/2026-second-half-replay-playbook.md`](../../2026-second-half-replay-playbook.md)
is reached. Leave a row blank rather than guessing; an unfilled row is a
valid, visible "not yet reached" state, not an error to hide. Database
archives, checkpoint files, and their private SHA-256 hashes are recorded
here by identity/hash only — the artefacts themselves stay outside the
repository (see the [README](README.md)'s evidence policy).

## Source first-half checkpoint (section C)

| Field | Value |
|---|---|
| Source archive identity (private filename, not committed) | |
| Source archive SHA-256 | |
| Source checkpoint JSON SHA-256 | |
| Checkpoint timestamp / stage | `2026-05-10T10:15:00Z` / `final-results` (from `phase-one-closeout.md`; confirm on verification) |
| Verified against `evidence-manifest.md` closing baseline | |

## Second-half working copy (section D)

| Field | Value |
|---|---|
| Working database | `bbbffl_2026_second_half` (project `bbbffl-2026-second-half`) |
| Working-copy backup identity at creation (SHA-256) | |
| `restored_from` (source archive SHA-256, section C) | |
| Application commit (`git rev-parse HEAD`) | |
| Migration head (`python -m app.migrations current`) | |
| Environment/configuration assumptions used | |
| Rounds 1–9 / squads / ladder / audit history confirmed intact | |

## Round 10 pre-draft checkpoint (section G)

| Field | Value |
|---|---|
| Backup identity (SHA-256) | |
| Checkpoint timestamp / stage | |
| Round 10 ladder verified | |
| Discrepancies recorded in `round-results.md` | |

## Post-mid-season-draft checkpoint (section I)

| Field | Value |
|---|---|
| Backup identity (SHA-256) | |
| Trigger round / competition ID used | |
| Final squad sizes verified | |
| Post-draft trading closed | |
| Exceptional Scorer corrections (if any) | |

## Round 20 / home-and-away checkpoint (section K)

| Field | Value |
|---|---|
| Backup identity (SHA-256) | |
| Checkpoint timestamp / stage | |
| Home-and-away ladder verified | |
| Known unresolved historical uncertainty | |

## AFL evidence acquisition (section E)

| Field | Value |
|---|---|
| Acquired package identity (`manifest.id` / `package_version`) | |
| Acquisition timestamp (`manifest.acquired_at`) | |
| Source API host (`manifest.source_api`, never credentials) | |
| Included AFL round identities (10–24 inclusive, fifteen rounds -- R10–20 for the ordinary second-half replay, R21–24 acquired now for the later finals/SuperScore replay) | |
| Match/stat/roster coverage (`validate` PASS output) | |
| `scripts.second_half_replay validate` result | |
| AFL-api disconnected after acquisition | |

## Known replay deviations / unresolved historical uncertainty

Record anything that could not be resolved with available evidence here, in
addition to the round-specific rows above.
