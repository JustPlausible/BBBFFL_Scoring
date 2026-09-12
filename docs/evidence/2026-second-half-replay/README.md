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

**Home-and-away replay complete through Round 20.**

Round 10 was replayed and verified before the reconstructed mid-season draft.
The draft then completed with 28 delistings and 28 selections, the historical
post-draft Crabs/Bridesmaids trade was restored through the supported audited
workflow, and all ten squads returned to 22 players before Round 11.

Rounds 11–20 subsequently completed through the normal weekly lineup, lockout,
calculation, Scorer review, publication and ladder lifecycle. A verified paired
end-of-home-and-away recovery point is retained privately by the operator for
the finals handoff.

The replay produced a coherent mathematical Round 20 ladder. Two known
historical Scorer-error results in Rounds 12 and 13 caused the actual 2026
finals order to differ from that reconstruction. Issue #187 / PR #188 therefore
preserved the mathematical ladder and created a separate immutable, audited,
replay-only historical finals-seeding snapshot. See
[`finals-seeding-2026.md`](finals-seeding-2026.md).

Issue #168 is the execution tracker closed by this evidence set. The next replay
phase is planned under issue #170 (finals and four-round SuperScore replay);
see [`docs/2026-finals-superscore-design.md`](../../2026-finals-superscore-design.md)
for the resulting design and follow-up issue decomposition.

## Documents

- [Provenance manifest](provenance-manifest.md) records the major replay
  boundaries and the sanitised verification state. Private database archives,
  checkpoint files and their integrity hashes remain outside the repository.
- [Round results](round-results.md) records Round 10, the mid-season boundary,
  Rounds 11–20, the closing mathematical ladder and material historical
  variance.
- [Workflow findings](workflow-findings.md) captures durable behavioural and
  operational findings that generalise beyond an individual round.
- [UX findings](ux-findings.md) captures coach/Scorer/replay usability findings
  discovered during execution.
- [Finals seeding 2026](finals-seeding-2026.md) records why the mathematical
  Round 20 ladder and historical finals order differ and how the replay-only
  audited seed snapshot preserves both.

## Evidence policy

Identical to the first-half evidence policy
(`docs/evidence/2026-first-half-replay/README.md`'s "Evidence policy"
section): sanitised commit/PR/issue references, non-personal AFL provider
identifiers, schema/migration names, package hashes, and sanitised behavioural
observations may be committed. Passwords, tokens, session values, private
config, private network addresses/host paths, raw coach communications,
screenshots, database dumps, checkpoint files, backup filenames, and their
private integrity hashes are retained privately by the operator and never
committed.
