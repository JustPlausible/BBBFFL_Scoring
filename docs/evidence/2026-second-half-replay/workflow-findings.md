# Workflow findings

## Verified behaviour through Round 20

The second-half replay continued directly from the completed first-half database
and verified the intended path from Round 10 through the end of the ordinary
home-and-away season:

1. restore the completed Round 9 database and paired replay checkpoint;
2. migrate that working copy to the current schema without losing prior 2026
   history;
3. extend the intentionally truncated nine-round replay fixture to the full
   twenty-round ordinary season;
4. replay and finalise Round 10 using the pre-mid-season squads;
5. preserve a verified pre-draft recovery point;
6. confirm the Round 10 ladder and reverse-ladder mid-season draft order;
7. record and lock historical delistings;
8. generate selection entitlement from squad vacancies;
9. complete all 28 historical mid-season draft selections;
10. reconstruct and approve the historical Crabs/Bridesmaids post-draft trade;
11. verify all ten squads returned to 22 players and close post-draft trading;
12. complete Round 11 as the first normal weekly lifecycle from the new squads;
13. replay Rounds 12–20 through normal lineup, lockout, scoring, review,
    sign-off/publication and ladder accumulation; and
14. preserve the mathematical Round 20 ladder and create a separate audited
    historical finals-seeding snapshot for the finals/SuperScore handoff.

Rounds 11–20 all reached `final`. The post-draft ownership state remained
usable throughout the remainder of the home-and-away replay.

## Durable findings

### Persisted BBBFFL history can outlive the active AFL replay package

The second-half working database legitimately contains final BBBFFL rounds whose
original AFL evidence belonged to the earlier first-half replay package. Final
BBBFFL history therefore remains readable from persisted competition state even
when the currently mounted AFL replay package begins at a later round.

Issue #176 / PR #177 corrected a dashboard assumption that the latest final
BBBFFL round must always be present in the active provider package.

### A truncated replay fixture needs an explicit continuation workflow

The first-half replay deliberately ended with nine ordinary rounds. Restoring
that database for Phase 2 correctly restored a nine-round season rather than an
automatically expandable twenty-round fixture.

Issue #178 / PR #179 added a supported continuation operation that preserved
existing round identities/history and appended Rounds 10–20 without weakening
normal frozen-fixture rules.

### The mid-season draft boundary must follow a final Round 10

Round 10 was completed entirely with the pre-mid-season squads. Only after its
results and ladder were final was the draft-specific ladder snapshot confirmed.
This prevents ownership changes from retroactively affecting the round that
sets draft order.

### Draft entitlement is derived from vacancies, not a second pick-count source

The replay recorded 28 historical delistings across nine teams. Running Hots
made no delistings and received no selections. Locking the delistings generated
exactly 28 selections across five draft rounds, with teams disappearing from
later rounds once their vacancy entitlement was exhausted.

### Delisted players genuinely return to the available pool

The historical draft exercised multiple ownership transitions successfully:
Errol Gulden moved from Pommy Rules to JHAS, Connor Rozee moved from Wolverines
to The Crabs, and Sam Durham was delisted and then re-drafted by The Crabs.
The latter demonstrates that a same-club re-draft is still a real release and
reacquisition even when a simple before/after squad comparison looks unchanged.

### Historical corrections should replay forward from a preserved boundary

The post-draft Crabs/Bridesmaids trade was rediscovered after post-draft trading
had initially been closed. The replay returned to the preserved post-draft,
pre-close database boundary, reproduced the trade through the supported domain,
verified it in an isolated restored database, and then rebuilt the canonical
state. No direct SQL mutation of the canonical history was required.

The final trade moved James Rowbottom to Bridesmaids and Lachlan McAndrew to
The Crabs.

### Database checkpoints are useful regression environments

A preserved PostgreSQL replay archive was restored into a disposable database
and used to exercise the historical trade approval path. This exposed a real
PostgreSQL defect (`FOR UPDATE` on an aggregate query), fixed by issue #182,
before the canonical replay was mutated.

Meaningful replay backups therefore serve both recovery and realistic
integration/regression testing purposes.

### A deterministic bye is not the same as unresolved availability evidence

Round 12 exposed historical lineups containing players from AFL clubs on a bye.
Ordinary submission should hard-block such a selection because the player has
no match in which to participate, but that invalid selection must not itself
lock the position before the real BBBFFL lockout.

Issue #185 / PR #186 separated selection validity from lock state. A confirmed
bye player remains an invalid selection that must be replaced before ordinary
submission, while the position stays editable until the applicable BBBFFL
lockout genuinely activates. Unresolved or stale evidence still fails closed.

For historical fidelity, the 2026 bye selections were restored through the
existing audited Scorer correction workflow rather than weakening production
submission rules.

### Routine delegated operation is a useful proxy for future coach operation

Rounds 11–20 were largely routine once the mid-season transition was complete.
Repeated delegated lineup entry, staged lockout evaluation, Scorer review,
publication and public verification demonstrated that the operational model is
credible for individual coaches performing their own weekly submissions in a
live season. The Scorer tools remained useful for exceptional cases without
becoming the normal path.

### Mathematical replay results and historical competition outcomes can both be true

The completed replay produced a coherent mathematical Round 20 ladder from the
current scoring engine and reconstructed authoritative evidence. Two known 2026
Scorer-error outcomes (Round 12 Evil Absolutes v Running Hots and Round 13
Motherruckers v Evil Absolutes) meant the historical finals order differed from
that mathematical ladder.

Issue #187 / PR #188 preserves both truths: the mathematical ladder remains
unchanged and inspectable, while an immutable replay-only finals-seeding
snapshot records the historical order for finals/SuperScore consumption. Small
PF/PA differences that did not alter match winners were intentionally not
manually reconciled.

### Replay recovery points must remain paired

As in the first half, a useful recovery point consists of both the database
archive and the matching replay checkpoint. The operator retained verified
paired boundaries during the second half, including the end-of-home-and-away
handoff before finals work begins.

## Implementation outcomes during the second-half replay

- Historical-dashboard reads across replay evidence boundaries: issue #176 / PR #177.
- Supported continuation from nine to twenty ordinary rounds: issue #178 / PR #179.
- Public ladder subtitle correction for future/scheduled rounds: issue #180.
- Mid-season-draft workflow and replay tooling matured through issues #164–#182.
- PostgreSQL trade-approval correction: issue #182.
- Bye-player editability with submission still fail-closed: issue #185 / PR #186.
- Replay-only historical finals-seeding snapshot: issue #187 / PR #188.

## Follow-up boundaries

- Issue #170 now owns planning and decomposition of the finals and four-round
  SuperScore replay phase.
- The mid-season draft UI remains a live-2027 usability improvement rather than
  a blocker for historical replay completion (issue #181).
- Coach-facing availability filtering, selector ordering preferences and other
  lineup usability improvements should remain advisory/user-controlled except
  where participation is deterministic (for example, an AFL-club bye).
- The completed 2026 database lineage should be retained as historical evidence
  rather than discarded when the 2027 live season is established.
