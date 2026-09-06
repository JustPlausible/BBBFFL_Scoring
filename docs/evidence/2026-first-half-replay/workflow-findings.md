# Workflow findings

This document records durable process findings from the 2026 first-half replay.
Implementation-specific details belong in the linked domain documentation and
issues; this is the evidence-led summary.

## Verified behaviour

| Area | Finding | Status |
|---|---|---|
| Bootstrap | Ten entries, accepted draft order, player pool, squad limit, nine logical rounds and Opening Round rules can be established reproducibly before drafting. | Verified |
| Draft | All 220 selections, reversions and finalisation completed across desktop, tablet and phone-sized views. | Verified; minor mobile layout finding |
| Opening squads | Transaction-window review, validation and immutable opening-squad freeze completed. | Verified |
| Opening Round | Sixty nominations across ten confirmed submissions were reconstructed. Zero or partial nomination sets remain valid. | Verified after Issues [#131](https://github.com/JustPlausible/BBBFFL_Scoring/issues/131), [#133](https://github.com/JustPlausible/BBBFFL_Scoring/issues/133) and [#135](https://github.com/JustPlausible/BBBFFL_Scoring/issues/135) |
| Mapping | Explicit BBBFFL-to-AFL mapping with an operator reason is authoritative even when logical round numbers differ. | Verified; selection UX remains manual |
| Lockout | A replay checkpoint advances evidence time; an authorised lineup request materialises durable trigger activation. Selective lockout protects only covered players, then main lockout protects all remaining positions. | Verified |
| Live submission | A round becomes lifecycle `live` at the first match while submissions remain valid for unlocked positions. | Verified after [#144](https://github.com/JustPlausible/BBBFFL_Scoring/issues/144) / PR [#145](https://github.com/JustPlausible/BBBFFL_Scoring/pull/145) |
| Locked correction | A Scorer/Admin can make a reasoned, immutable correction to an existing authoritative submission without weakening ordinary lock enforcement. | Verified after [#137](https://github.com/JustPlausible/BBBFFL_Scoring/issues/137) / PR [#142](https://github.com/JustPlausible/BBBFFL_Scoring/pull/142) |
| Missed initial submission | A saved pre-lockout draft can support a narrowly authorised first-submission adjudication when per-position evidence proves the locked values. External league consultation remains outside the app. | Implemented by [#146](https://github.com/JustPlausible/BBBFFL_Scoring/issues/146) / PR [#149](https://github.com/JustPlausible/BBBFFL_Scoring/pull/149); workflow verified, but not used to fabricate evidence for an operator omission |
| Final evidence | Scoring must wait until the replay releases final results for the current AFL round and every earlier source round used by deferred players. | Verified |
| Scorer review | Missing-stat evidence remains conservative: without a stat row or independent participation fact, DNP cannot be inferred and requires a scorer ruling. | Verified |
| Publication | Atomic sign-off publishes all matchups and updates public round results and the cumulative ladder. | Verified through Round 4 |

## Operational lessons

### Lifecycle and replay evidence are separate

The BBBFFL lifecycle (`open`, `live`, `review`, `final`) and replay evidence
stage (`scheduled`, `final-results`) serve different purposes. A round can be
correctly `live` while replay evidence remains `scheduled`. Scoring requires
both the BBBFFL lifecycle to be `review` and the necessary AFL source rounds to
be released as final evidence.

### Trigger materialisation must be visible

Changing the checkpoint does not, by itself, persist a trigger activation. A
lineup/lockout read evaluates the authoritative facts and records the durable
activation. Operator guidance or a dashboard should make that evaluation and
its result explicit instead of relying on knowledge of which page causes it.

### Carry-forward stays conservative

Exact carry-forward must refuse when a carried position conflicts with an
Opening Round deferred nomination. The system should not silently merge,
relocate or reinterpret players. Human review is preferable for uncommon
historical exceptions.

### Corrections must preserve history

Direct database edits and backward checkpoint changes are not correction
workflows. Existing submissions use audited locked-lineup correction. A missed
initial submission may use evidenced-draft adjudication only where the app
actually retained qualifying evidence. A replay-operator transcription omission
without such evidence requires restoration to an earlier consistent database
and checkpoint.

### Historical standings may differ from recomputation

Current authoritative AFL statistics can differ from a contemporaneous manual
spreadsheet because of original entry error or later provider revision. Future
mid-season/finals reconstruction may need an audited distinction between the
calculated ladder and a historical seeding position. Any such facility must
preserve both states and state why the historical override was used.

## Outstanding workflow work

- Provide explicit Scorer and Administrator operational dashboards: Issues
  [#147](https://github.com/JustPlausible/BBBFFL_Scoring/issues/147) and
  [#148](https://github.com/JustPlausible/BBBFFL_Scoring/issues/148).
- Review whether historical ladder/seeding reconstruction requires a narrowly
  scoped audited workflow before the mid-season draft or finals replay.
- Update replay guidance so final-evidence checkpoints include every deferred
  source round required by the target BBBFFL round.
- Provide safer backup/checkpoint pairing guidance for destructive replay
  restoration.
