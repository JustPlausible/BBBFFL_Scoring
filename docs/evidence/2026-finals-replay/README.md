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

**Tooling and documentation complete; live replay execution pending
operator access to the 2026 replay environment.**

Issue #194 delivered the audit-event catalogue addendum
(`docs/audit-events.md`), the operational playbook
(`docs/2026-finals-superscore-playbook.md`), the three previously-missing
operator CLIs (`scripts/superscore_round_2026.py`,
`scripts/superscore_review_2026.py`, `scripts/season_completion_2026.py`),
the final-archival-checkpoint guard (`app/season_archival.py`,
`scripts/season_archival_checkpoint_2026.py`, both with passing automated
tests), and a fix to a genuine PostgreSQL defect in `app.superscore_round`
that would otherwise have made the SuperScore round-setup CLI unusable
against the real replay database.

**What this issue did not do, and why:** this development session has no
access to the actual 2026 finals/SuperScore replay database or Docker
Compose stack (`compose.second-half-replay.yaml`'s successor installation)
-- that environment is operator-only, holds real private secrets, and is
never available inside CI/development. Concretely, this means:

- The post-finals-seeding-apply paired database/checkpoint backup (below)
  has **not** been taken against the real replay database. The exact
  procedure to take it is documented in full in
  [`provenance-manifest.md`](provenance-manifest.md) and
  `docs/2026-finals-superscore-playbook.md` section C; running it is
  outstanding operator work.
- No finals week or SuperScore round has actually been played against the
  real 2026 replay database in this session. This directory therefore has
  no `round-results.md`/`ux-findings.md` populated with real findings yet
  -- [`workflow-findings.md`](workflow-findings.md) instead records what
  this issue found and fixed in the *code/tooling* while preparing the
  operator surface (the missing CLIs, the PostgreSQL bug), which is real,
  verifiable work product, distinct from replay execution findings.
- The hard exit-gate checklist (`docs/2026-finals-superscore-playbook.md`)
  is consequently unchecked -- it cannot honestly be marked complete
  without the operator having actually run the phase.

## Documents

- [Provenance manifest](provenance-manifest.md) -- the post-finals-seeding-
  apply backup record (template, outstanding), the per-finals-week and
  per-SuperScore-round checkpoint template, and the final archival
  checkpoint template bound to issue #195's completion-event identifier.
- [Workflow findings](workflow-findings.md) -- durable findings from
  preparing this phase's operator surface: the missing SuperScore round-
  setup/review and season-completion CLIs, and the PostgreSQL `COUNT(*) ...
  FOR UPDATE` defect in `app.superscore_round`.

`round-results.md`/`ux-findings.md` are intentionally not created yet --
`docs/2026-finals-superscore-playbook.md`'s own instructions direct the
operator to create them, in the same shape as the second-half replay's own
documents, once the phase is actually run.

## Evidence policy

Identical to the first-half/second-half evidence policy: sanitised
commit/PR/issue references, non-personal AFL provider identifiers,
schema/migration names, package hashes, and sanitised behavioural
observations may be committed. Passwords, tokens, session values, private
config, private network addresses/host paths, raw coach communications,
screenshots, database dumps, checkpoint files, backup filenames, and their
private integrity hashes are retained privately by the operator and never
committed.
