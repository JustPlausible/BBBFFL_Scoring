# BBBFFL round to AFL context mapping

BBBFFL competition rounds and AFL rounds are separate identities. Each mapping
belongs to one persisted `bbbffl_round`, and therefore inherits its competition
stream and BBBFFL season scope. Ordinary finals and SuperScore may independently
point at the same public `afl-api-v1` season and round IDs.

Mappings are append-only revisions. A setup revision is explicitly `unresolved`
or `ambiguous` and `resolve()` deliberately returns no operational context for
it. Acceptance is the only activation boundary and first verifies the IDs via
the public versioned AFL API contract. It never consults the current AFL round,
compares round numbers, or applies an offset.

An accepted revision is frozen. Setup/default changes cannot edit it. An
authorised correction requires a reason, appends a new accepted revision, moves
the current pointer, and records before/after state in the common append-only
audit log. The prior revision remains queryable as mapping history.

Migration `0008_round_map` promotes each unambiguous legacy
`bbbffl_round_afl_reference` row to accepted revision 1 while retaining its
mapping ID, provider IDs and timestamp, then removes the legacy table. A legacy
round with multiple references stops the upgrade for an explicit ruling rather
than silently selecting one. `RoundMappingRepository` is consequently the only
writable and resolvable mapping boundary after upgrade.

## 2026 evidence

The 2026 workbook findings establish 20 ordinary rounds followed by a four-week
finals series, while the 24-round AFL home-and-away structure places those
finals in AFL rounds 21–24. Thus BBBFFL finals week 4 (the Grand Final) is a
supported exceptional identity mapping rather than a numeric-equality case.

The planning evidence also says Opening Round performances were historically
deferred to a club's later bye, but does not fully specify a generally safe
mapping rule. Such setup is represented as ambiguous and remains non-operational
rather than inventing a rule. Modelling match-level/deferred-fact composition,
if confirmed, belongs in a separate follow-up rather than this round-context
foundation.

## Evidence-backed mapping recommendations (issue #152)

The Round Preflight workflow (see [`round-preflight.md`](round-preflight.md))
lets an operator pick an AFL season and round from human-readable labels
instead of copying opaque provider IDs, and offers a deterministic
*recommended* mapping wherever one can be established with confidence:
`app.round_mapping.recommend_mapping` looks for exactly one AFL season whose
published year matches the BBBFFL season's year, and within it exactly one
AFL round whose published round number matches the BBBFFL round's sequence.

This recommendation is a read-only, advisory-only helper, entirely separate
from `accept()`/`correct()` above, which remain exactly as ambivalent to it
as this document already describes -- acceptance never consults the current
AFL round, never compares round numbers, and never applies an offset, and
never applies a recommendation automatically either. `recommend_mapping`
fails closed (returns no recommendation) rather than guessing whenever the
evidence is ambiguous or incomplete, which is expected and common. It is
also explicitly **gated to the ordinary (home-and-away) competition
stream**: the 2026 evidence above establishes that finals week 4 maps to
AFL round 24, not "round 4" -- and unlike the ordinary stream, a finals or
SuperScore stream's own sequence numbering restarts independently of AFL's,
so an AFL season can easily happen to *also* publish an unrelated round
numbered 4. Applying the equal-number heuristic there would recommend that
real-but-wrong round with the same false confidence as a correct one; gating
by stream type means non-ordinary rounds instead never receive a
recommendation, leaving the operator's own deliberate identification of the
correct AFL context as the only authoritative source, exactly as this
document already requires. An operator remains free -- and, for
finals-style rounds, required -- to accept a mapping that diverges from any
recommendation shown, provided they explicitly confirm it and give a reason
(see `round-preflight.md`).

## Whole-round mapping vs. player-level deferred scoring (issue #69)

That follow-up is [`opening-round-deferred-selection.md`](opening-round-deferred-selection.md).
It models a **separate, player-level** scoring-source override that can
coexist with this module's whole-round mapping in the same BBBFFL round: an
individual lineup slot with an active Opening Round nomination
(`app.opening_round`) draws its statistics from the player's AFL Opening
Round match, while `RoundMappingRepository`'s accepted mapping continues to
supply every other slot's AFL context exactly as before. This module never
gains Opening Round awareness itself -- a round's whole-round mapping and a
slot's deferred source are deliberately independent boundaries; see that
document for the full design, the 2024/2025/2026 evidence (which disproves
any general `Opening Round -> R2..R4` assumption -- 2024's compensating
byes extend to R5/R6), and why the exact historical BBBFFL nomination
remains unresolved.
