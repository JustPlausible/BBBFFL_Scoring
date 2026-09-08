# Round replay results

Status at completion of the 2026 first-half replay. All nine ordinary BBBFFL
rounds reached `final`, were published, and contained ten authoritative
weekly-lineup submissions.

| BBBFFL round | AFL round | Selective match | Main match | Outcome |
|---:|---:|---:|---:|---|
| 1 | 1344 | 8045 | 8044 | Pass; selective/main lockout, review and publication verified |
| 2 | 1345 | 8052 | 8053 | Pass; Opening Round deferred values and audited correction verified |
| 3 | 1346 | 8062 | 8064 | Pass; recovery rehearsal and post-lockout correction path verified |
| 4 | 1347 | 8066 | 8067 | Pass; final Opening Round deferred values resolved |
| 5 | 1348 | 8072 | 8076 | Pass; human-readable operational labels verified |
| 6 | 1349 | 8083 | 8085 | Pass; externally authorised late change recorded through audited correction |
| 7 | 1350 | 8093 | 8097 | Pass; routine weekly lifecycle completed |
| 8 | 1351 | 8104 | 8102 | Pass; routine weekly lifecycle completed |
| 9 | 1352 | 8113 | 8116 | Pass; carry-forward fallback applied for a missing submission |

All trigger activations were persisted with `match_time_reached` evidence.
The one-second offset used in later replay checkpoints deliberately placed the
authoritative replay clock just beyond the scheduled lock instant; it did not
alter the persisted effective lock time.

## Opening Round milestone

Opening Round (AFL round 1343) was not treated as a normal BBBFFL fixture
round. Its preloaded nominations remained locked and were consumed as deferred
score evidence in the nominated compensating round. The last such values were
resolved during BBBFFL Round 4.

## Exceptional workflow evidence

- A submitted lineup could be corrected after lockout only through the audited
  Scorer correction workflow and a substantive recorded reason.
- A team with no first authoritative submission could be resolved only through
  the missed-submission adjudication workflow. Evidenced draft acceptance was
  limited to provable pre-lock values; carry-forward did not merge rejected
  private-draft content.
- In Round 6, a historical league decision made outside the application was
  represented correctly by an audited post-lockout correction. No in-app
  quorum mechanism was required.
- In Round 9, the rules-based carry-forward used the previous effective
  submitted lineup. A historical worksheet had instead introduced a player who
  was not present in that lineup. The replay retained the rule-correct result.
  The matchup winner was unchanged, although aggregate points, percentage or
  points-per-game may differ from the historical worksheet.

## Closing ladder

| Rank | Team | Pts | W | D | L | PF | PA | % |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | Running Hots | 28 | 7 | 0 | 2 | 1771 | 1526 | 116.1 |
| 2 | Evil Absolutes | 24 | 6 | 0 | 3 | 1713 | 1625 | 105.4 |
| 3 | JHAS | 20 | 5 | 0 | 4 | 1717 | 1665 | 103.1 |
| 4 | Motherruckers | 16 | 4 | 0 | 5 | 1695 | 1640 | 103.4 |
| 5 | The Crabs | 16 | 4 | 0 | 5 | 1659 | 1656 | 100.2 |
| 6 | One Percenters | 16 | 4 | 0 | 5 | 1623 | 1677 | 96.8 |
| 7 | The Plague | 16 | 4 | 0 | 5 | 1571 | 1670 | 94.1 |
| 8 | Bridesmaids | 16 | 4 | 0 | 5 | 1585 | 1686 | 94.0 |
| 9 | Pommy Rules | 16 | 4 | 0 | 5 | 1721 | 1884 | 91.3 |
| 10 | Wolverines | 12 | 3 | 0 | 6 | 1567 | 1593 | 98.4 |

The table is a replay closeout observation, not a replacement for the
authoritative published ladder. Ladder order is competition points, percentage,
then PF. Points per game may be displayed for interest, but it is derived from
PF and is not an additional ordering criterion. Exact equality after the three
published criteria requires a recorded, audited Scorer decision rather than an
invented automatic tiebreaker.

## Historical-stat variance

Small differences were observed between current provider statistics and the
values in the historical shared worksheet. No replay overrides were introduced
where they did not change the verified match outcome. Before draft or finals
seeding is derived from the replay ladder, any material variance must be
reconciled explicitly and recorded as an evidence-backed administrative
decision rather than silently editing calculated results.
