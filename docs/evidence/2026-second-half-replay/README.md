# 2026 second-half replay evidence

This directory is the sanitised project record for the 2026 BBBFFL
second-half replay (Round 10, the reconstructed mid-season draft, and
Rounds 11–20). It follows the same evidence policy and document shape as
[`docs/evidence/2026-first-half-replay/`](../2026-first-half-replay/), which
it continues directly from.

The operational procedure lives in
[`docs/2026-second-half-replay-playbook.md`](../../2026-second-half-replay-playbook.md).
This directory records what the procedure actually found when executed; it
is not a copy of private league communications, authentication material,
host configuration, database backups, screenshots, or the complete
chronological operator journal.

## Status

Not yet executed. This directory currently holds the provenance template and
blank test log the playbook's sections C–O reference as replay proceeds.
Issue [#166](https://github.com/JustPlausible/BBBFFL_Scoring/issues/166)
executes Round 10 against the working database this playbook establishes;
its findings, and every later round's, land here.

## Documents

- [Provenance manifest](provenance-manifest.md) is the template for the
  checkpoint-identity records the playbook's section M requires at each
  major boundary (first-half source checkpoint, second-half working copy,
  pre-draft, post-draft, and Round 20 checkpoints).
- [Round results](round-results.md) is the blank Round 10–20 test log and
  findings record, in the same style as the first-half
  [`round-results.md`](../2026-first-half-replay/round-results.md).

Workflow and UX findings that generalise beyond a single round should be
added here following the first-half
[`workflow-findings.md`](../2026-first-half-replay/workflow-findings.md) and
[`ux-findings.md`](../2026-first-half-replay/ux-findings.md) pattern once
the replay has produced any — they are not pre-created empty here to avoid
implying findings exist before they do.

## Evidence policy

Identical to the first-half evidence policy
(`docs/evidence/2026-first-half-replay/README.md`'s "Evidence policy"
section): sanitised commit/PR/issue references, non-personal AFL provider
identifiers, schema/migration names, package hashes, and sanitised
behavioural observations may be committed. Passwords, tokens, session
values, private config, private network addresses/host paths, raw coach
communications, screenshots, database dumps, checkpoint files, backup
filenames, and their private integrity hashes are retained privately by the
operator and never committed.
