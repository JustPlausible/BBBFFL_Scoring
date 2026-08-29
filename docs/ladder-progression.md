# Deterministic ladder progression

The BBBFFL ladder is a derived read model. `app.ladder.LadderRepository`
rebuilds a requested season's table through an inclusive round boundary; no
ladder counter or editable ladder table is source of truth.

## Sporting rules

Each win awards **4 competition points**, each draw **2**, and each loss **0**.
Played, wins, draws, losses, Points For (PF), and Points Against (PA) are summed
from matchup scores. The authoritative historical workbook formula is:

```text
percentage = (PF / PA) * 100
points per game = PF / Played
```

Consistent with the legacy implementation, percentage and PPG are reported as
zero when their denominator is zero. Calculation retains Decimal precision;
rounding is presentation work and is not used for ladder ordering.

Sporting order is descending (1) competition points, (2) percentage, then (3)
PF. There is deliberately no fourth criterion. Rows equal on all three share
the same sporting rank and expose `tied=True` and the same `tie_group`. Entry ID
is used only to serialize such a group repeatably and does not rank one tied
club above another.

## Effective results and snapshots

The repository joins `bbbffl_matchup.effective_official_version` to that exact
immutable `bbbffl_official_result` version. It also requires the persisted
round to be `final`. Consequently live scores, calculated snapshots,
unfinalised review data, and superseded official versions cannot contribute.
A scorer-approved correction appends an official version and moves the existing
effective pointer, so the next rebuild changes naturally while the old version
remains in official-result history.

Every request is season-scoped and includes only ordinary results whose fixture
round is at or before the requested boundary. A `LadderSnapshot` contains the
boundary, rows, and compact `(matchup_id, official_version)` references for
every contributing result. Repeating a build with the same effective inputs
therefore returns an identical snapshot, and later rounds cannot leak backwards.

## Frozen downstream decisions

Recomputation produces competition information; it does **not** update a frozen
mid-season draft order, finals qualification/seeding, or another downstream
ruling. Those future aggregates must retain the ladder snapshot/provenance they
were frozen from and require their own explicit correction workflow.

## 2026 evidence boundary

The repository does not contain the 2026 workbook itself. The preserved
workbook findings and legacy GAS establish the formula, 4/2/0 awards, and the
three ordering criteria, but do not provide a machine-readable round-by-round
2026 score corpus. The database-backed 2026 replay test therefore validates a
labelled synthetic official-result round and correction against those confirmed
properties (including every row's W/D/L, PF/PA, points, percentage, rank/tie,
and provenance where applicable). It must not be represented as genuine coach
history. Importing the workbook's real round scores can extend the same replay
surface without changing production ladder rules.
