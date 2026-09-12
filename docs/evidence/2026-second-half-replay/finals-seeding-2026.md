# 2026 finals-seeding snapshot: two truths, one bridge (issue #187)

This document explains why the completed 2026 second-half replay carries
**two** distinct, simultaneously true records of the Round 20 home-and-away
season, and how the replay bridges from one into the finals/SuperScore
phase without ever collapsing them into a single "corrected" ladder.

## The two records

1. **The mathematical Round 20 ladder.** `app.ladder.calculate_ladder`,
   applied to the reconstructed authoritative AFL evidence and the current
   scoring engine, through the normal audited match/result lifecycle
   (`docs/2026-second-half-replay-playbook.md` sections F–K). This is the
   ladder Season Centre / Round Centre display, and it is never rewritten by
   anything in this document.
2. **The historical 2026 finals-seeding order.** The order the real 2026
   BBBFFL competition actually used to seed finals after Round 20. It
   differs from (1) for three teams because of two known historical
   Scorer-error match outcomes (below).

Repairing the two historical match results to make (1) agree with (2) is
explicitly out of scope (issue #169/#187): that would destroy the
mathematical reconstruction's own evidentiary value, and no other
score/PF/PA discrepancy across the replay is reconciled this way either.
Instead, issue #187 (`app.finals_seeding`) records (2) as a single,
explicit, immutable, audited **finals-seeding snapshot**, scoped only to
this 2026 replay season, that the finals/SuperScore replay (issue #170)
consumes in place of recalculating seeds from (1) -- while (1) remains
exactly as `app.ladder` computed it, permanently inspectable.

## Why the two orders differ: Round 12 and Round 13

### Round 12 -- Evil Absolutes v Running Hots

| | Evil Absolutes | Running Hots | Winner |
|---|---:|---:|---|
| Reconstructed/current application | 161 | 149 | Evil Absolutes |
| Historical 2026 competition | 159 | 163 | **Running Hots** |

### Round 13 -- Motherruckers v Evil Absolutes

| | Motherruckers | Evil Absolutes | Winner |
|---|---:|---:|---|
| Reconstructed/current application | 176 | 182 | Evil Absolutes |
| Historical 2026 competition | 182 | 181 | **Motherruckers** |

Together, these two outcome differences exactly explain the material
W/L/competition-points divergence for the three affected teams -- no other
team's finals position is affected, and no other historical score
discrepancy (there are several minor PF/PA variances elsewhere in the
replay; see `round-results.md`'s "Historical-stat variance" section) changed
any match winner:

| Team | Mathematical (Round 20 ladder) | Historical (2026 competition) |
|---|---|---|
| Running Hots | 12-8, 48 pts | 13-7, 52 pts |
| Evil Absolutes | 12-8, 48 pts | 10-10, 40 pts |
| Motherruckers | 8-12, 32 pts | 9-11, 36 pts |

## The required historical finals-seeding order

1. Running Hots
2. Bridesmaids
3. JHAS
4. Wolverines
5. Evil Absolutes
6. The Crabs
7. One Percenters
8. Motherruckers
9. Pommie/Pommy Rules
10. The Plague

`app.finals_seeding.HISTORICAL_FINALS_SEED_TEAM_NAMES` is the single source
of this order in code; it resolves each name to this season's own
`season_entry_id` at snapshot time (never storing a bare display-name
string as the seed itself), so a later team rename does not silently
invalidate the recorded snapshot.

## Mechanism summary (see `app.finals_seeding` for the full design)

- **Preview** (`scripts/finals_seeding_2026.py preview`) is read-only: it
  reports whether the season is the intended 2026 replay context, whether
  Round 20 is complete, the current mathematical order, the proposed
  historical order, the three teams' material W/L/points differences, this
  rationale, and whether a snapshot already exists / can be applied. It
  never writes anything.
- **Apply** (`scripts/finals_seeding_2026.py apply`) requires an explicit
  `--season-id`, `--competition-id`, and substantive `--reason`. It fails
  closed (no mutation) unless: the season's year is exactly 2026, the named
  competition is that season's own ordinary home-and-away stream, and every
  round through 20 is `final`. It accepts **no caller-supplied seed order**
  -- the only order it can ever write is the fixed historical order above --
  so it cannot become a general ladder-correction tool, and it has no
  season/environment opt-in that could ever let the live 2027 season use it.
  Re-applying with an unchanged resolution is a no-op (idempotent); a
  resolution that no longer matches an existing snapshot fails closed
  (`FinalsSeedingConflictError`) rather than silently overwriting it.
- **Audit** every successful apply is one `finals_seeding.snapshot.created`
  audit event (`app.audit`) recording the actor, timestamp, the caller's
  reason, the frozen mathematical order (`before_state`), the historical
  seed order (`after_state`), and this rationale (`payload.
  historical_rationale`).
- **Finals consumption** `app.finals_seeding.resolve_finals_seed_order`
  is the one seam a future finals/SuperScore implementation (issue #170)
  calls for its seed order: it returns this snapshot's order when one
  exists for the requested season/competition, and otherwise falls back,
  completely unchanged, to the ordinary mathematical ladder order --
  exactly what every season without an authorised replay snapshot (2027
  included) always does.

## What this is not

- Not a rewrite of the Round 12/13 official results, or of any other
  historical score/PF/PA figure -- those stay exactly as the replay
  produced them, and remain useful reconstruction evidence in their own
  right.
- Not a claim that the historical spreadsheet's PF/PA figures are
  authoritative -- only the *seed order itself* is taken as a deliberate
  historical-fidelity decision for the finals replay.
- Not a general ladder-correction or ladder-override feature, and not
  available to a coach-facing surface or the live 2027 season -- see
  `app.finals_seeding`'s module docstring for the structural reasons why.
