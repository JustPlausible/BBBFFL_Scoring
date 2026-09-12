# Round replay results

Status at completion of the 2026 second-half home-and-away replay. Round 10,
the reconstructed mid-season boundary, and Rounds 11–20 completed without
resetting or replacing the first-half history. Every ordinary round reached
`final` and was published through the normal weekly lifecycle.

| BBBFFL round | AFL round | Outcome |
|---:|---:|---|
| 10 | 1353 | Pass; pre-mid-season squads, review/publication and ladder verified |
| Mid-season draft | — | Pass; 28 delistings, 28 selections, historical post-draft trade restored, all squads 22 |
| 11 | 1354 | Pass; first normal post-draft weekly lifecycle completed from reconstructed squads |
| 12 | 1355 | Pass with finding; historical bye-player selections reproduced through audited Scorer correction; issue #185 / PR #186 improved pre-lock correction UX |
| 13 | 1356 | Pass; routine lifecycle completed; one historical winner differs from mathematical replay and is recorded below |
| 14 | 1357 | Pass; routine weekly lifecycle completed |
| 15 | 1358 | Pass; routine weekly lifecycle completed |
| 16 | 1359 | Pass; routine weekly lifecycle completed; verified paired recovery point retained privately |
| 17 | 1360 | Pass; routine weekly lifecycle completed |
| 18 | 1361 | Pass; routine weekly lifecycle completed |
| 19 | 1362 | Pass; routine weekly lifecycle completed |
| 20 | 1363 | Pass; home-and-away replay completed and mathematical ladder preserved for finals handoff |

The second-half rounds exercised staged selective/main lockouts, delegated
lineup entry, scoring, DNP/interchange review where required, Scorer sign-off,
publication and public ladder accumulation. No loss/reset of earlier 2026
history was required.

## Mid-season draft milestone

Round 10 was final before any mid-season ownership changes were applied. The
replay then:

- confirmed the reverse-ladder draft order;
- recorded 28 historical delistings across nine teams;
- generated exactly 28 vacancy-derived selections across five draft rounds;
- completed all 28 historical picks;
- restored the later-discovered historical player trade (James Rowbottom to
  Bridesmaids; Lachlan McAndrew to The Crabs) through the supported proposal
  and approval workflow;
- verified all ten squads at 22 players; and
- closed post-draft trading before Round 11.

A corrected post-mid-season database archive and matching team-list snapshot
were retained privately and independently integrity-verified before the replay
continued.

## Exceptional workflow evidence

- **Post-draft trade recovery:** the historical Crabs/Bridesmaids trade was
  discovered after trading had initially been closed. The replay restored the
  preserved pre-close boundary, tested the trade in an isolated PostgreSQL
  database, fixed the PostgreSQL-specific approval defect under issue #182,
  then replayed the canonical trade forward through the supported audited
  workflow.
- **Round 12 bye selections:** historical lineups contained players from AFL
  clubs on a bye. Ordinary submission correctly rejected those impossible
  selections. After issue #185 / PR #186 made the invalid positions editable
  before actual lockout, the historical selections were restored only through
  the authorised Scorer lineup-correction workflow with substantive replay
  reasons. Public lineup state reflected the resulting authoritative corrected
  versions.
- **Finals seeding:** the mathematical Round 20 ladder was deliberately left
  untouched. Issue #187 / PR #188 created a separate immutable replay-only
  historical finals-seeding snapshot after the two known historical
  winner-changing Scorer errors were identified.

## Closing mathematical ladder (after Round 20)

| Rank | Team | Pts | W | D | L | PF | PA | % |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | Bridesmaids | 52 | 13 | 0 | 7 | 3832 | 3714 | 103.2 |
| 2 | Running Hots | 48 | 12 | 0 | 8 | 3893 | 3535 | 110.1 |
| 3 | Evil Absolutes | 48 | 12 | 0 | 8 | 3790 | 3590 | 105.6 |
| 4 | JHAS | 48 | 12 | 0 | 8 | 3675 | 3617 | 101.6 |
| 5 | Wolverines | 44 | 11 | 0 | 9 | 3645 | 3606 | 101.1 |
| 6 | The Crabs | 40 | 10 | 0 | 10 | 3642 | 3555 | 102.4 |
| 7 | One Percenters | 36 | 9 | 0 | 11 | 3509 | 3536 | 99.2 |
| 8 | Motherruckers | 32 | 8 | 0 | 12 | 3607 | 3741 | 96.4 |
| 9 | Pommy Rules | 28 | 7 | 0 | 13 | 3586 | 3881 | 92.4 |
| 10 | The Plague | 24 | 6 | 0 | 14 | 3486 | 3890 | 89.6 |

This table is the mathematical replay closeout observation and remains
inspectable after the historical finals-seeding snapshot is created.

## Historical-stat variance

Small PF/PA differences were observed between current provider-derived scoring
and the historical spreadsheet throughout the replay. They were not manually
reconciled when they did not alter a matchup winner.

Two historical Scorer-error outcomes did alter winners and therefore the finals
order:

### Round 12 — Evil Absolutes v Running Hots

- mathematical replay: Evil Absolutes 161, Running Hots 149 — Evil Absolutes win;
- historical competition: Evil Absolutes 159, Running Hots 163 — Running Hots win.

### Round 13 — Motherruckers v Evil Absolutes

- mathematical replay: Motherruckers 176, Evil Absolutes 182 — Evil Absolutes win;
- historical competition: Motherruckers 182, Evil Absolutes 181 — Motherruckers win.

Together these explain the material W/L/competition-points differences for
Running Hots (12-8/48 mathematical vs 13-7/52 historical), Evil Absolutes
(12-8/48 vs 10-10/40), and Motherruckers (8-12/32 vs 9-11/36).

Issue #187 / PR #188 therefore records the historical finals order in a
separate audited snapshot rather than editing these mathematical results or
forcing unrelated PF/PA values to match the spreadsheet. See
[`finals-seeding-2026.md`](finals-seeding-2026.md).

## Historical finals seed order

The finals/SuperScore replay will consume the audited historical snapshot in
this order:

1. Running Hots
2. Bridesmaids
3. JHAS
4. Wolverines
5. Evil Absolutes
6. The Crabs
7. One Percenters
8. Motherruckers
9. Pommy Rules
10. The Plague
