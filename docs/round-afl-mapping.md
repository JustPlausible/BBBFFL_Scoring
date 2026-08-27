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
