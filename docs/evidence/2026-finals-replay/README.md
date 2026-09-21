# 2026 finals and SuperScore replay evidence

This directory is the sanitised project record for the 2026 BBBFFL finals
and SuperScore replay phase (main finals bracket, weeks 1-4; SuperScore
rounds SS1-SS4; end-of-season completion). It follows the same evidence
policy and document shape as
[`docs/evidence/2026-second-half-replay/`](../2026-second-half-replay/) and
[`docs/evidence/2026-first-half-replay/`](../2026-first-half-replay/).

## Why a new directory, not an extension of `2026-second-half-replay/`

`docs/2026-finals-superscore-design.md`'s "Backup/recovery" note and
`docs/2026-second-half-replay-playbook.md` section L both explicitly offer
either choice ("extend `docs/evidence/2026-second-half-replay/` or a new
`2026-finals-replay` evidence directory"). Issue #194 chose the new
directory, for the same reason the first-half and second-half replays
already each have their own:

- `docs/evidence/2026-second-half-replay/README.md`'s "Status" section
  reads **"Home-and-away replay complete through Round 20"** -- a closed,
  verified record of a phase that actually finished. Writing finals/
  SuperScore provenance into that directory would either reopen a "complete"
  status document mid-phase or force an awkward second "status" section
  inside one file.
- The finals/SuperScore phase introduces new document *kinds* this
  directory's own convention doesn't have a slot for without new headings:
  a post-finals-seeding-apply backup record, per-finals-week and
  per-SuperScore-round checkpoints, and a final archival checkpoint tied to
  a specific completion-event identifier (see below) -- materially
  different in shape from a home-and-away round-by-round record.
- Each replay phase boundary (first half, second half, finals/SuperScore)
  already gets its own playbook document
  (`2026-first-half-replay-playbook.md`, `2026-second-half-replay-playbook.md`,
  `2026-finals-superscore-playbook.md`); a matching one-phase-one-evidence-
  directory convention keeps the playbook-to-evidence mapping obvious.

This directory's own provenance manifest cross-links back to
`2026-second-half-replay/provenance-manifest.md`'s "Round 20 / home-and-away
boundary" and "Finals-seeding snapshot" entries as its own starting point,
exactly as the second-half manifest itself cross-links back to the
first-half `phase-one-closeout.md`.

## Status

**Replay execution and season closeout completed on 2026-09-19.**

The real 2026 replay environment was run through all four Finals weeks and
all four SuperScore rounds, including authenticated Coach submissions,
exceptional Scorer workflows, Grand Final/SS4 publication, season
completion, independent archival verification, and a final validated
database/checkpoint archive. The sanitised completion identifiers and
verification state are recorded in
[`provenance-manifest.md`](provenance-manifest.md); private backup
filenames, paths and hashes remain outside GitHub by policy.

The replay also surfaced execution-time findings beyond the original
tooling work: the parent season remained in `setup` until closeout and had
to be transitioned through the supported audited `setup -> active` path
before completion; browser Scorer workflows proved preferable for routine
Finals operation; Coach privacy/eligibility boundaries and several
exceptional Scorer workflows were exercised; and several non-blocking UX
follow-ups were identified. These are recorded in
[`workflow-findings.md`](workflow-findings.md).

Some earlier per-round recovery-boundary details were not captured into
this repository at the time they were taken. Where exact audit-event ids or
checkpoint metadata are no longer available from the conversation record,
the manifest now records that evidence gap explicitly rather than
inventing identifiers. The terminal pre-closeout and post-completion
archives are both preserved and validated privately.

## Documents

- [Provenance manifest](provenance-manifest.md) -- sanitised replay
  provenance, known evidence gaps, season-completion identifiers, and the
  final archival-checkpoint verification.
- [Workflow findings](workflow-findings.md) -- tooling findings plus
  execution findings from the completed Finals/SuperScore replay,
  including lifecycle, Coach/privacy, Scorer workflow and UX observations.
- [Round results](round-results.md) -- phase-level execution summary for
  Finals Weeks 1-4 and SS1-SS4, including the historical-result outcome
  checks available from the replay record.
- [UX findings](ux-findings.md) -- operator/Coach usability observations
  retained for 2027 follow-up.
- [Full-season replay summary](../2026-full-season-replay-summary.md)
  synthesises this phase with the first-half and second-half evidence into
  one full-season view, and links to the current
  [2027 live-season readiness](../../2027-live-season-readiness.md)
  document.

## Evidence policy

Identical to the first-half/second-half evidence policy: sanitised
commit/PR/issue references, non-personal AFL provider identifiers,
schema/migration names, package hashes, and sanitised behavioural
observations may be committed. Passwords, tokens, session values, private
config, private network addresses/host paths, raw coach communications,
screenshots, database dumps, checkpoint files, backup filenames, and their
private integrity hashes are retained privately by the operator and never
committed.
